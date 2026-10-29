# Stage 4C: observe GitHub before proposing a push

`git-plan remote --branch stage/...` checks the clean local branch and exact
configured origin, then reads the remote branch SHA. An absent branch yields
`null`; a transport or authentication error fails closed.

`git-plan prepare --branch stage/...` emits a complete `git.push` operation
plan with current local and observed remote SHA. The plan is reviewable and
can be passed to the local `operation-plan validate` and `approve` commands.
Save it outside the Git tree. It is invalid if the local branch changes.

Neither command pushes or changes Git refs. A future runner must enforce
expected remote state at the effect, approval, lease and postcondition. The
public MCP viewer has no Git write capability.
