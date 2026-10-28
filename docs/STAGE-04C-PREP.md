# Stage 4C: Git destination guard

`git-plan inspect --branch stage/...` is a local, read-only preflight. It checks
the named stage branch, current source SHA, clean working tree, exact GitHub
origin fetch and push URLs, and rejects mirror or implicit push refspecs. It
returns an explicit non-force refspec for review. A dirty tree is left intact.

This is preparation for WORK-04-03. It neither commits nor pushes. A future
push runner must require a current exact-plan operator approval, a continuity
lease, a fresh remote-head check, and observed post-push evidence. GitHub
credentials remain outside Git. Do not mark WORK-04-03 complete from this
preflight alone.
