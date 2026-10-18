import os
import tempfile

_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_db.name}",
    "LLM_FAKE": "1",
    "DEV_MODE": "1",
    "BASE_URL": "http://testserver",
    "RESEND_API_KEY": "",
    "RESEND_WEBHOOK_SECRET": "",
})
