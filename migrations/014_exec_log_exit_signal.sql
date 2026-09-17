-- 014_exec_log_exit_signal.sql
-- The signal that killed an ephemeral run's command, when one did (#197). The
-- exit code alone cannot say it: a command the guest's OOM killer took and one
-- that exited 137 on its own are the same number, and before this the host read
-- asyncssh's exit_status straight through and recorded -1. NULL is a command
-- that exited on its own status, which is every row written before this.
ALTER TABLE exec_log ADD COLUMN exit_signal TEXT;
