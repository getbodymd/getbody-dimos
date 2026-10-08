import logging

# Background task threads can log after a test's captured stream has closed
# (dimos's late reply to a cancelled move_to). That is expected; don't print
# logging's own traceback for it.
logging.raiseExceptions = False
