import os

# The API tests swap in a fake portal after startup; don't let the app try to
# warm its snapshot from the real one first.
os.environ.setdefault("WARM_ON_STARTUP", "0")
