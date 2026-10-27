"""Minimal fakes for handler-level tests.

No Telegram network objects are involved: these classes only reproduce the
attributes and async methods the handlers actually touch.
"""


class FakeUser:
    def __init__(self, user_id: int = 123456789) -> None:
        self.id = user_id


class FakeChat:
    def __init__(self, chat_id: int = 123456789) -> None:
        self.id = chat_id


class FakeBot:
    """Fake bot that serves a fixed payload for downloads."""

    def __init__(self, payload: bytes = b"payload", fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail
        self.downloads = []

    async def download(self, file_id, destination=None):
        self.downloads.append(file_id)
        if self.fail:
            raise RuntimeError("download failed")
        if destination is not None:
            destination.write(self.payload)
        return destination


class FakeMessage:
    """Collects everything a handler sends."""

    def __init__(self, user_id: int = 123456789) -> None:
        self.sent = []
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(user_id)
        self.document = None
        self.photo = None
        self.bot = FakeBot()

    async def answer(self, text, reply_markup=None, disable_web_page_preview=None):
        self.sent.append({"text": text, "reply_markup": reply_markup})
        return self

    async def answer_photo(self, *args, **kwargs):
        self.sent.append({"text": "[photo]", "reply_markup": None})
        return self

    async def answer_document(self, document, caption=None, **kwargs):
        self.sent.append(
            {
                "text": caption or "[document]",
                "reply_markup": None,
                "document": document,
            }
        )
        return self


class FakeCallback:
    """Fake CallbackQuery with a message and answer() bookkeeping."""

    def __init__(self, user_id: int = 123456789) -> None:
        self.message = FakeMessage(user_id)
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