"""Minimal fakes for handler-level tests.

No Telegram network objects are involved: these classes only reproduce the
attributes and async methods the handlers actually touch.
"""


class FakeMessage:
    """Collects everything a handler sends."""

    def __init__(self) -> None:
        self.sent = []

    async def answer(self, text, reply_markup=None, disable_web_page_preview=None):
        self.sent.append({"text": text, "reply_markup": reply_markup})
        return self

    async def answer_photo(self, *args, **kwargs):
        self.sent.append({"text": "[photo]", "reply_markup": None})
        return self


class FakeUser:
    def __init__(self, user_id: int = 123456789) -> None:
        self.id = user_id


class FakeCallback:
    """Fake CallbackQuery with a message and answer() bookkeeping."""

    def __init__(self, user_id: int = 123456789) -> None:
        self.message = FakeMessage()
        self.from_user = FakeUser(user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append({"text": text, "show_alert": show_alert})


def button_datas(markup):
    """Flatten inline keyboard callback_data values for assertions."""
    if markup is None:
        return []
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]