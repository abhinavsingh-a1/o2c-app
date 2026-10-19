import os

# erp_mock.db reads this at import time; tests override get_session, so the
# engine built from it is never used.
os.environ.setdefault("ERP_DATABASE_URL", "sqlite://")
