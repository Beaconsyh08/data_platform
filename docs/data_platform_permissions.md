# Data Platform execution and episode deletion permissions

## Service identities

Web roles and Linux file permissions serve different purposes. Both admin and operator jobs execute
under the Agent service account. An admin login cannot overcome a filesystem `PermissionError`.
Keep separate, stable service accounts for prod and dev; service accounts should retain a `nologin`
shell. Use an administrator's SSH account for host maintenance, and `sudo -u <agent-user>` to check
the permissions of the actual worker.

Source datasets need read and directory traversal access. Viewer caches, sibling output parents and
mutation backup directories need write access. Enabled source mutations also need write access to
the source. Retain the Server A and Agent source-mutation switches and registered-root validation.

When migrating service identities, migrate service-owned cache files to the new service identity,
including existing CSVs and videos. Back up ownership and ACLs with `getfacl -R -p` before changes;
`setfacl --restore=<backup>` restores the recorded ownership and permissions when run as root.
Do not recursively change source dataset ownership or grant world write access. An ACL's mask can
disable named grants; inspect `getfacl` as well as `namei -l`. A restrictive temporary-file creation
mode can mask inherited ACL grants, so a shared group or default ACL alone does not ensure that
files created by a previous service account remain readable.
The cache temporary-file helper now creates unique files exclusively with mode `0666`, letting the
worker's umask and directory default ACL set effective access. Atomic replacement is preserved.
For caches owned by data producers, grant the prod Agent a named ACL and default directory ACL;
preserve other entries' effective permissions when adjusting an existing restrictive ACL mask.

Dev uses its own dataset and output roots. Do not add the dev Agent to a production data group merely
to resolve a disabled frontend button. Verify read/write access as the correct service account.

## Episode deletion approval

Operators can run supported sibling transforms, including Merge. Operators cannot directly invoke
source-deletion endpoints. To request partial deletion on an Agent dataset:

1. Select the Agent dataset in **Datasets**.
2. Open **Runs → Episode deletion requests**, enter explicit episode indices and a reason, then
   click **Request episode deletion**. Submission saves a pending request; it deletes no data.
3. An admin reviews the dataset, episode indices and reason in the same panel, enters a review
   reason, and approves or rejects the request.
4. Approval creates an existing `mutation.delete_episodes` job. The Server A and Agent mutation
   switches must be enabled. The Agent retains its backup, recovery and Viewer invalidation logic.
5. The operator sees the decision and execution job ID. Check that job in Runs for completion or
   failure; approval alone does not mean deletion succeeded.

All operator deletion requests require approval, including datasets they produced. Legacy datasets
do not have reliable platform ownership, so filesystem owners are not treated as web-account owners.
Admin direct mutation controls retain their existing guards and confirmation requirements.
The request mechanism currently covers registered Agent datasets, not Server A's local-only registry.

Requests are immutable after submission. Dataset registration metadata is checked again during
approval; if it changed, submit a fresh request. Approval and job creation commit in one transaction;
repeat decisions return a conflict. Agent claim checks persisted approval and its active admin
reviewer, rather than trusting an approval flag in the submitted payload. Operators see their own
requests and jobs; admins see all. Request and review events use the existing audit outbox.

The release needs the additive `dp_episode_deletion_requests` control-plane table, created by the
existing environment schema initialization workflow. Apply the normal dev acceptance and approved
production release process before enabling this UI in production.

These restrictions govern platform/API operations. Existing Linux accounts with write access to
shared dataset directories still have their Linux permissions; web roles do not restrict SSH access.

## Submission feedback

Console mutations show a persistent dismissible result on every page. Job acceptance includes its
ID and queue status; rejected requests show the server's error. Network errors and timeouts report
that delivery could not be confirmed and direct the user to check Runs before retrying.
Remote Merge uses the Agent's capabilities rather than Server A's optional preprocessing imports.
Disabled split/merge controls display their missing prerequisite beside the button.
Merge checks the datasets selected in its source list. An unrelated Working dataset with an
unsupported signal layout does not disable merging supported sources; an unsupported selected
source is reported by name.

## Dataset scopes

Admins can edit **Data permissions** for viewers, operators and data managers in Users & access.
Viewer/operator accounts default to all current and future registered datasets. Selected-only mode
limits their dataset visibility, direct Viewer/cache access and task inputs, including every Merge
source. An empty selection grants no dataset access. Only admins may change scopes; role capabilities
remain unchanged. Data-manager scopes continue to govern source mutations rather than read access.

Queued tasks recheck current access before Agent claim. Scope revocation does not interrupt a task
already running. Newly produced datasets need an explicit grant in selected-only mode. Restricted
accounts cannot browse or register arbitrary server filesystem datasets or read the global operation
log; administrators register and grant the required locations. Source-local datasets without a
registered control-plane location cannot be selected. Existing explicit grants are preserved.
Lifecycle/curation APIs use version/workspace IDs without registered-location scope mapping; they
require unrestricted access until that mapping exists, rather than exposing out-of-scope artifacts.

### AI 上下文助手

Viewer 可在其数据权限范围内使用只读 AI 解读；操作角色可生成流程草稿和准备操作。
聊天接口不注册任务提交、重试、审批、删除或发布工具；最终动作仍使用原入口的授权与确认。
任务解释遵守原有 owner/admin 可见性，Quality 报告沿用 Curation 的范围限制。
服务端在读取上下文、返回结果和恢复对话时重查权限及对象身份；降为 Viewer 后不返回可应用草稿。
所有账号共享现有用户/全局并发及额度限制；密钥仅在服务端配置。

AI 不具有源码、服务器文件、SQL 查询或任意 HTTP 访问工具，也不能以聊天中声称的管理员身份扩权。
索取源码、内部指令、数据库导出或凭据的典型请求由服务端拒绝；常见编码和零宽字符绕过也纳入检查。
发往模型的消息、历史及工具摘要统一脱敏；回复、历史恢复和请求回执同样过滤已配置的敏感值、
常见凭据格式与服务器路径。模型输出命中敏感内容时丢弃该回复和草稿，显示固定拒绝提示。
AI 草稿不接受文件路径、输出目录、网络地址或凭据参数；需要这些参数时使用已有手动入口及权限检查。
安全审计只记录拒绝类别和请求身份，不记录命中的敏感值；助手响应禁用浏览器缓存，异常不回显堆栈或 SQL。
这些检测是纵深防护，不能保证识别所有语言或编码方式；核心边界仍是最小化摘要、对象授权和固定工具白名单。
历史记录在读取和再次使用时过滤；不会追溯清理既有数据库备份，也不会替代已泄露凭据的轮换。
