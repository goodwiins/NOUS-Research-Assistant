"""One process-wide lock for PyMuPDF (``fitz``).

PyMuPDF is not thread-safe: two threads inside it at once can crash the
process or corrupt document state. Every caller that may run in a thread
(API worker threads, ``asyncio.to_thread``) opens and reads documents only
while holding ``PYMUPDF_LOCK``. It is re-entrant, so a helper that takes it
can call another helper that takes it too.

Callers reference it as ``pdf_lock.PYMUPDF_LOCK`` (not a from-import) so
tests can swap it. Never ``await`` while holding it.
"""

import threading

PYMUPDF_LOCK = threading.RLock()
