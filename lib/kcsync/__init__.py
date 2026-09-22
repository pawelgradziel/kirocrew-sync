"""kcsync - the data engine behind kirocrew-sync's three-way merge.

Responsibilities are split deliberately:

  * this package  - unpack/pack SQLite + files to/from a canonical text form,
                    and perform row-level three-way merges
  * kirocrew-sync.sh - git orchestration and backend transport

Nothing here talks to a storage backend. The one network peer is the local
KiroCrew gateway on this machine, which mailbox.py calls for session
export/import (send-session / inbox).
"""

FORMAT_VERSION = 1

__all__ = ["FORMAT_VERSION"]
