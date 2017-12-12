from common.yavide_utils import YavideUtils
from services.service_plugin import ServicePlugin

class VimClangTidy(ServicePlugin):
    def __init__(self, yavide_instance):
        self.yavide_instance = yavide_instance

    def startup_callback(self, success, payload):
        YavideUtils.call_vim_remote_function(self.yavide_instance, "Y_ClangTidy_StartCompleted()")

    def shutdown_callback(self, success, payload):
        reply_with_callback = bool(payload)
        if reply_with_callback:
            YavideUtils.call_vim_remote_function(self.yavide_instance, "Y_ClangTidy_StopCompleted()")

    def __call__(self, success, args, payload):
        YavideUtils.call_vim_remote_function(self.yavide_instance, "Y_ClangTidy_Apply('" + args + "')")
