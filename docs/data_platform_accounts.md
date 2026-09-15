# Central accounts and password recovery

Central accounts use one `username` for registration, login, account labels, and user search.
User IDs, roles, approval state, and password hashes are preserved. New API responses and database
schemas do not contain `display_name`. The Python store temporarily accepts the old optional
`display_name` argument for caller compatibility and ignores it.

## Change a known password

Open **Account → Change password**, or **Platform management → Change password**. Enter the current
password and the new password twice (10–256 characters). Saving revokes every session for this
account and invalidates outstanding reset links. Sign in again with the new password. Viewer users
can change their own password. Exit development role preview before changing passwords.

## Recover a forgotten password

1. The user contacts an administrator through the team's existing private communication channel.
2. After verifying identity, the administrator opens **Platform management → Users → Reset password**.
3. Confirm generation, select/copy the link, and send it privately to that user.
4. The user opens the link and chooses their own password. The administrator does not see it.
5. After saving, the user signs in again. All prior sessions and reset links are invalidated.

Links expire after 15 minutes, can be consumed once, and are replaced when a new link is issued.
Issuing a link does not change the password or sign the user out. Resetting does not approve or
reactivate an account, or change its role. The token is stored as a hash, placed in the URL fragment
rather than the query string, and removed from browser history when the page opens. Reloading that
page requires reopening the original link. Closing the administrator dialog clears the displayed link.

Changes, issuance, and completion use the existing audit system without recording credentials.
Database-backed counters allow ten requests per five-minute window for each acting user (change or
issue) or client address (anonymous reset), across workers. Standard environment CSRF and maintenance
guards also apply. The final password update, token consumption, and session revocation are atomic.

## API

- `POST /api/auth/password`: `current_password`, `new_password`, `confirm_password`; signed-in account.
- `POST /api/auth/users/<user_id>/password-reset`: administrator only; returns `token`, `expires_in`.
- `POST /api/auth/reset-password`: `token`, `new_password`, `confirm_password`; no login required.
- `GET /account/password` and `GET /reset-password`: password forms.

## Development deployment and migration

Use the normal immutable release flow with explicit `--env dev`. From a clean, committed checkout:

```bash
sudo bash deploy/data-platform/update-server.sh --env dev --version accounts-YYYYMMDD-N
```

The release workflow stops the selected environment after jobs drain, backs it up, runs schema
initialization, and restarts it. Initialization adds `dp_password_resets` and
`dp_password_rate_limits` and drops the legacy `dp_users.display_name` column idempotently. Existing
usernames, user IDs, password digests, and sessions survive the migration. Only successful password
changes revoke sessions. Do not run the migration against an old running web process.

The dropped display names cannot be recovered by switching code back. To return to code requiring
that column, use the release workflow's explicit matching backup restoration. This change does not
require an Agent update. Production deployment remains a separate release action.
