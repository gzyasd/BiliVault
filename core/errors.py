class BibiError(Exception):
    code: str = "UNKNOWN"
    user_message: str = "发生未知错误"

    def __init__(self, user_message: str | None = None, *, code: str | None = None):
        if user_message:
            self.user_message = user_message
        if code:
            self.code = code
        super().__init__(self.user_message)


class NotLoggedInError(BibiError):
    code = "NOT_LOGGED_IN"
    user_message = "B站登录已失效，请重新扫码登录"


class BiliApiError(BibiError):
    code = "BILI_API_ERROR"

    def __init__(self, bili_code: int, message: str):
        self.bili_code = bili_code
        super().__init__(f"B站接口错误({bili_code}): {message}", code="BILI_API_ERROR")


class AiApiError(BibiError):
    code = "AI_API_ERROR"


class StateError(BibiError):
    code = "STATE_ERROR"
