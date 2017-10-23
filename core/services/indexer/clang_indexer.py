import logging
import multiprocessing
import os
import shlex
import subprocess
import sqlite3
import time
import tempfile
from services.parser.ast_node_identifier import ASTNodeId
from services.parser.clang_parser import ChildVisitResult
from services.parser.clang_parser import ClangParser

# TODO move this to utils
import itertools
def slice_it(iterable, n, padvalue=None):
    return itertools.izip_longest(*[iter(iterable)]*n, fillvalue=padvalue)

class SymbolDatabase(object):
    def __init__(self, db_filename = None):
        if db_filename:
            self.db_connection = sqlite3.connect(db_filename)
        else:
            self.db_connection = None

    def __del__(self):
        if self.db_connection:
            self.db_connection.close()

    def open(self, db_filename):
        if not self.db_connection:
            self.db_connection = sqlite3.connect(db_filename)

    def close(self):
        if self.db_connection:
            self.db_connection.close()

    def get_all(self):
        # TODO Use generators
        return self.db_connection.cursor().execute('SELECT * FROM symbol')

    def get_by_id(self, id):
        return self.db_connection.cursor().execute('SELECT * FROM symbol WHERE usr=?', (id,))

    def insert_single(self, filename, unique_id, line, column, symbol_type):
        self.db_connection.cursor().execute('INSERT INTO symbol VALUES (?, ?, ?, ?, ?)', (filename, unique_id, line, column, symbol_type,))

    def flush(self):
        self.db_connection.commit()

    def delete(self, filename):
        self.db_connection.cursor().execute('DELETE FROM symbol WHERE filename=?', (filename,))

    def delete_all(self):
        self.db_connection.cursor().execute('DELETE FROM symbol')

    def create_data_model(self):
        self.db_connection.cursor().execute('CREATE TABLE IF NOT EXISTS symbol_type (id integer, name text, PRIMARY KEY(id))')
        self.db_connection.cursor().execute('CREATE TABLE IF NOT EXISTS symbol (filename text, usr text, line integer, column integer, type integer, PRIMARY KEY(filename, usr, line, column), FOREIGN KEY (type) REFERENCES symbol_type(id))')
        symbol_types = [(1, 'function'), (2, 'variable'), (3, 'user_defined_type'), (4, 'macro'),]
        self.db_connection.cursor().executemany('INSERT INTO symbol_type VALUES (?, ?)', symbol_types)


class ClangIndexer(object):
    def __init__(self, parser, callback = None):
        self.callback = callback
        self.db = None
        self.indexer_directory_name = '.indexer'
        self.indexer_db_name = 'indexer.db'
        self.cpu_count = multiprocessing.cpu_count()
        self.proj_root_directory = None
        self.compiler_args = None
        self.parser = parser
        self.op = {
            0x0 : self.__run_on_single_file,
            0x1 : self.__run_on_directory,
            0x2 : self.__drop_single_file,
            0x3 : self.__drop_all,
            0x10 : self.__go_to_definition,
            0x11 : self.__find_all_references
        }


    def __call__(self, args):
        self.op.get(int(args[0]), self.__unknown_op)(int(args[0]), args[1:len(args)])

    def __unknown_op(self, id, args):
        logging.error("Unknown operation with ID={0} triggered! Valid operations are: {1}".format(id, self.op))

    def __run_on_single_file(self, id, args):
        proj_root_directory = str(args[0])
        contents_filename = str(args[1])
        original_filename = str(args[2])
        compiler_args = str(args[3])

        self.db = sqlite3.connect(os.path.join(self.proj_root_directory, self.indexer_db_name))

        if contents_filename == original_filename:
            index_single_file(self.parser, proj_root_directory, contents_filename, original_filename, compiler_args, self.db)

        if self.callback:
            self.callback(id, args)

    def __run_on_directory(self, id, args):
        # NOTE  Indexer will index each file in directory in a way that it will:
        #           1. Index a file
        #           2. Flush its AST immediately to the disk
        #           3. Repeat 1 & 2 for each file
        #
        #       One might notice that 2nd step could have been:
        #           1. Run after whole directory has been indexed
        #              (which is possible because we keep all the translation units in memory)
        #           2. Skipped and executed on demand through a separate API (if and when client wants to)
        #
        #       Both approaches have been evaluated and it turned out that 'separate API' approach lead to
        #       very high RAM consumption (>10GB) which would eventually render the indexer non-functional
        #       for any mid- to large-size projects.
        #
        #       For example, running an indexer on a rather smallish project (cppcheck, ~330 files at this moment)
        #       would result in:
        #           1. RAM consumption of ~5GB if we would parse all of the files _AND_ flush the ASTs to the disk.
        #              The problem here is that RAM consumption would _NOT_ go any lower even after the ASTs have been
        #              flushed to disk which was strange enough ...
        #           2. RAM consumption of ~650MB if we would load all of the previously parsed ASTs from the disk.
        #       There is a big discrepency between these two numbers which clearly show that there is definitely some
        #       memory lost in the process.
        #
        #       Analysis of high RAM consumption has shown that issue was influenced by a lot of small object artifacts
        #       (small memory allocations), which are:
        #           1. Generated by the Clang-frontend while running its parser.
        #           2. Still laying around somewhere in memory even after parsing has been completed.
        #           3. Accumulating in size more and more the more files are parsed.
        #           4. Not a subject to memory leaks according to the Valgrind but rather flagged as 'still reachable' blocks.
        #           5. Still 'occupying' a process memory space even though they have been 'freed'.
        #               * It is a property of an OS memory allocator to decide whether it will or it will not swap this memory
        #                 out of the process back to the OS.
        #               * It does that in order to minimize the overhead/number of dynamic allocations that are potentially
        #                 to be made in near future and, hence, reuse already existing allocated memory chunk(s).
        #               * Memory allocator can be forced though to claim the memory back to the OS through
        #                 'malloc_trim()' call if supported by the OS, but this does not guarantee us to get to
        #                  the 'original' RAM consumption.
        #
        #       'Flush-immeditelly-after-parse' approach seems to not be having these issues and has a very low memory
        #       footprint even with the big-size projects.

        self.proj_root_directory = str(args[0])
        self.compiler_args = str(args[1])

        # Do not run indexer on whole directory if we already did it
        directory_already_indexed = True
        indexer_directory_full_path = os.path.join(self.proj_root_directory, self.indexer_db_name)
        if not os.path.exists(indexer_directory_full_path):
            directory_already_indexed = False

        # Otherwise, index the whole directory
        self.db = sqlite3.connect(os.path.join(self.proj_root_directory, self.indexer_db_name))
        if not directory_already_indexed:
            logging.info("Starting to index whole directory '{0}' ... ".format(self.proj_root_directory))
            self.db = sqlite3.connect(os.path.join(self.proj_root_directory, self.indexer_db_name))
            self.db.cursor().execute('CREATE TABLE IF NOT EXISTS symbol_type (id integer, name text, PRIMARY KEY(id))')
            self.db.cursor().execute('CREATE TABLE IF NOT EXISTS symbol (filename text, usr text, line integer, column integer, type integer, PRIMARY KEY(filename, usr, line, column), FOREIGN KEY (type) REFERENCES symbol_type(id))')
            symbol_types = [(1, 'function'), (2, 'variable'), (3, 'user_defined_type'), (4, 'macro'),]
            self.db.cursor().executemany('INSERT INTO symbol_type VALUES (?, ?)', symbol_types)
            start = time.clock()
            cpp_file_list = []
            for dirpath, dirs, files in os.walk(self.proj_root_directory):
                for file in files:
                    name, extension = os.path.splitext(file)
                    if extension in ['.cpp', '.cc', '.cxx', '.c', '.h', '.hh', '.hpp']:
                        cpp_file_list.append(os.path.join(dirpath, file))

            # We will need a full path to 'clang_index.py' script
            this_script_directory = os.path.dirname(os.path.realpath(__file__))
            clang_index_script = os.path.join(this_script_directory, 'clang_index.py')

            # We will also need to setup a correct PYTHONPATH in order to run 'clang_index.py' script from another process(es)
            my_env = os.environ.copy()
            my_env["PYTHONPATH"] = os.path.dirname(os.path.dirname(this_script_directory))

            process_list = []
            tmp_db_list = []

            # We will slice the input file list into a number of chunks which corresponds to the amount of available CPU cores
            how_many_chunks = len(cpp_file_list)/self.cpu_count

            # Now we are able to parallelize the indexing operation across different CPU cores
            for cpp_file_list_chunk in slice_it(cpp_file_list, how_many_chunks):
                # 'slice_it()' utility function may return None's as part of the slice (to fill up the slice up to the given length)
                chunk_with_no_none_items = ', '.join(item for item in cpp_file_list_chunk if item)

                # Each subprocess will get an empty DB file to record indexing results into it
                handle, tmp_db = tempfile.mkstemp(suffix='.indexer.db', dir=self.proj_root_directory)

                # Start indexing a given chunk in a new subprocess
                #   Note: Running and handling subprocesses as following, and not via multiprocessing.Process module,
                #         is done intentionally and more or less it served as a (very ugly) workaround because of several reasons:
                #           (1) 'libclang' is not made thread safe which is why we want to utilize it from different
                #               processes (e.g. each process will get its own instance of 'libclang')
                #           (2) Python bindings for 'libclang' implement some sort of module caching mechanism which basically
                #               contradicts with the intent from (1)
                #           (3) Point (2) seems to be a Pythonic way of implementing modules which basically obscures
                #               the way how different instances of libraries (modules?) across different processes
                #               should behave
                #           (4) Python does have a way to handle such situations (module reloading) but seems that it
                #               works only for the simplest cases which is unfortunally not the case here
                #           (5) Creating a new process via subprocess.Popen interface and running the indexing operation
                #               from another Python script ('clang_index.py') is the only way how I managed to get it
                #               working correctly (each process will get their own instance of library)
                cmd = "python2 " + clang_index_script + " --project_root_directory='" \
                    + self.proj_root_directory + "' --compiler_args='" + self.compiler_args + "' --filename_list='" \
                    + chunk_with_no_none_items + "' --output_db_filename='" + tmp_db + "' " + "--log_file='" + \
                    logging.getLoggerClass().root.handlers[0].baseFilename + "'"
                p = subprocess.Popen(shlex.split(cmd), env=my_env)

                # Store handles to subprocesses and corresponding DB files so we can handle them later on
                process_list.append(p)
                tmp_db_list.append((handle, tmp_db))

            # Wait subprocesses to finish with their work
            for p in process_list:
                p.wait()

            # Merge the results of indexing operations (each process created a single indexing DB)
            logging.info('about to start merging the databases ... ' + str(tmp_db_list))
            for handle, db in tmp_db_list:
                conn = sqlite3.connect(db)
                query_result = conn.execute('SELECT * FROM symbol')
                if query_result:
                    for row in query_result:
                        self.db.execute('INSERT INTO symbol VALUES (?, ?, ?, ?, ?)', (row[0], row[1], row[2], row[3], row[4],))
                self.db.commit()
                conn.close()
                os.close(handle)
                os.remove(db)

            # TODO how to count total CPU time, for all processes?
            time_elapsed = time.clock() - start
            logging.info("Indexing {0} took {1}.".format(self.proj_root_directory, time_elapsed))
        else:
            logging.info("Directory '{0}' already indexed ... ".format(self.proj_root_directory))

        if self.callback:
            self.callback(id, args)

    def __drop_single_file(self, id, args):
        # TODO For each indexer table:
        #       1. Remove symbols defined from file to be dropped
        if self.callback:
            self.callback(id, args)

    def __drop_all(self, id, dummy = None):
        # TODO Drop data from all tables
        if self.callback:
            self.callback(id, dummy)

    def __go_to_definition(self, id, args):
        cursor = self.parser.get_definition(
            self.parser.parse(
                str(args[0]),
                str(args[0]),
                self.compiler_args,
                self.proj_root_directory
            ),
            int(args[1]), int(args[2])
        )
        if cursor:
            logging.info('Definition location {0}'.format(str(cursor.location)))
        else:
            logging.info('No definition found.')

        if self.callback:
            self.callback(id, cursor.location if cursor else None)

    def __find_all_references(self, id, args):
        start = time.clock()
        references = []
        tunit = self.parser.parse(str(args[0]), str(args[0]), self.compiler_args, self.proj_root_directory)
        if tunit:
            cursor = self.parser.get_cursor(tunit, int(args[1]), int(args[2]))
            if cursor:
                logging.info("Finding all references of cursor [{0}, {1}]: {2}. name = {3}".format(cursor.location.line, cursor.location.column, tunit.spelling, cursor.displayname))
                usr = cursor.referenced.get_usr() if cursor.referenced else cursor.get_usr()
                ast_node_id = self.parser.get_ast_node_id(cursor)
                if ast_node_id in [ASTNodeId.getFunctionId(), ASTNodeId.getMethodId()]:
                    query_result = self.db.cursor().execute("SELECT * FROM symbol WHERE usr=?", (usr,))
                elif ast_node_id in [ASTNodeId.getClassId(), ASTNodeId.getStructId(), ASTNodeId.getEnumId(), ASTNodeId.getEnumValueId(), ASTNodeId.getUnionId(), ASTNodeId.getTypedefId()]:
                    query_result = self.db.cursor().execute("SELECT * FROM symbol WHERE usr=?", (usr,))
                elif ast_node_id in [ASTNodeId.getLocalVariableId(), ASTNodeId.getFunctionParameterId(), ASTNodeId.getFieldId()]:
                    query_result = self.db.cursor().execute("SELECT * FROM symbol WHERE usr=?", (usr,))
                elif ast_node_id in [ASTNodeId.getMacroDefinitionId(), ASTNodeId.getMacroInstantiationId()]:
                    query_result = self.db.cursor().execute("SELECT * FROM symbol WHERE usr=?", (usr,))
                else:
                    query_result = None

                if query_result:
                    for row in query_result:
                        references.append((row[0], row[1], row[2], row[3]))
                        logging.debug('row: ' + str(row))

                time_elapsed = time.clock() - start
                logging.info('Find-all-references operation of {0} took {1}: {2}'.format(cursor.displayname, time_elapsed, str(references)))

        if self.callback:
            self.callback(id, references)

def index_file_list(proj_root_directory, compiler_args, filename_list, output_db_filename):
    db = sqlite3.connect(output_db_filename)
    db.cursor().execute('CREATE TABLE IF NOT EXISTS symbol_type (id integer, name text, PRIMARY KEY(id))')
    db.cursor().execute('CREATE TABLE IF NOT EXISTS symbol (filename text, usr text, line integer, column integer, type integer, PRIMARY KEY(filename, usr, line, column), FOREIGN KEY (type) REFERENCES symbol_type(id))')
    symbol_types = [(1, 'function'), (2, 'variable'), (3, 'user_defined_type'), (4, 'macro'),]
    db.cursor().executemany('INSERT INTO symbol_type VALUES (?, ?)', symbol_types)

    parser = ClangParser()
    for filename in filename_list:
        index_single_file(parser, proj_root_directory, filename, filename, compiler_args, db)
    db.close()


def index_single_file(parser, proj_root_directory, contents_filename, original_filename, compiler_args, db):
    def visitor(ast_node, ast_parent_node, parser):
        if (ast_node.location.file and ast_node.location.file.name == tunit.spelling):  # we are not interested in symbols which got into this TU via includes
            id = parser.get_ast_node_id(ast_node)
            usr = ast_node.referenced.get_usr() if ast_node.referenced else ast_node.get_usr()
            line = int(parser.get_ast_node_line(ast_node))
            column = int(parser.get_ast_node_column(ast_node))
            try:
                if id in [ASTNodeId.getFunctionId(), ASTNodeId.getMethodId()]:
                    db.cursor().execute('INSERT INTO symbol VALUES (?, ?, ?, ?, ?)', (tunit.spelling, usr, line, column, 1,))
                elif id in [ASTNodeId.getClassId(), ASTNodeId.getStructId(), ASTNodeId.getEnumId(), ASTNodeId.getEnumValueId(), ASTNodeId.getUnionId(), ASTNodeId.getTypedefId()]:
                    db.cursor().execute('INSERT INTO symbol VALUES (?, ?, ?, ?, ?)', (tunit.spelling, usr, line, column, 3,))
                elif id in [ASTNodeId.getLocalVariableId(), ASTNodeId.getFunctionParameterId(), ASTNodeId.getFieldId()]:
                    db.cursor().execute('INSERT INTO symbol VALUES (?, ?, ?, ?, ?)', (tunit.spelling, usr, line, column, 2,))
                elif id in [ASTNodeId.getMacroDefinitionId(), ASTNodeId.getMacroInstantiationId()]:
                    db.cursor().execute('INSERT INTO symbol VALUES (?, ?, ?, ?, ?)', (tunit.spelling, usr, line, column, 4,))
                else:
                    pass
            except sqlite3.IntegrityError:
                pass
            return ChildVisitResult.RECURSE.value  # If we are positioned in TU of interest, then we'll traverse through all descendants
        return ChildVisitResult.CONTINUE.value  # Otherwise, we'll skip to the next sibling

    logging.info("Indexing a file '{0}' ... ".format(original_filename))

    # TODO Indexing a single file does not guarantee us we'll have up-to-date AST's
    #       * Problem:
    #           * File we are indexing might be a header which is included in another translation unit
    #           * We would need a TU dependency tree to update influenced translation units as well

    # Index a single file
    start = time.clock()
    tunit = parser.parse(contents_filename, original_filename, str(compiler_args), proj_root_directory)
    if tunit:
        # TODO only if executed from index_single_file()
        #db.cursor().execute('DELETE FROM symbol WHERE filename=?', (tunit.spelling,))
        parser.traverse(tunit.cursor, parser, visitor)
        db.commit()
    time_elapsed = time.clock() - start
    logging.info("Indexing {0} took {1}.".format(original_filename, time_elapsed))

