# 云端安全配置凭据

网页对话中的 `request_secret_input` 使用独立凭据通道。配置成功后继续原来的
LocalSession 和工具调用，不重发任务。普通 `setup_required` 提示不是待输入请求，
必须先由模型调用 `request_secret_input` 创建待输入 Future。

## 交互与保存

网页收到 `agent.secret_required` 后显示配置卡片。弹窗默认使用密码输入框，完整多行
CDS 配置可以直接粘贴；显示凭据后可在多行输入框中编辑。说明和获取凭据的链接来自
客户端注册的 CredentialSpec。提交内容不经过聊天接口。

客户端先用注册的 consumer 保存凭据到当前 Agent 所在电脑，然后将一次性句柄交给
原工具；这个句柄只包含保存结果，不再保存原文，也不会二次写入凭据。解析或保存失败
不唤醒原 Future；旧请求失败，新请求提供重填入口。错误不回显提交内容。

## 加密协议

每个请求独立生成 RSA-2048 密钥。私钥仅保留在等待函数内存中，公钥使用 SPKI DER
Base64。浏览器随机生成 AES-256 密钥和 12 字节 IV，以 AES-GCM 加密 UTF-8 凭据。
AAD 为无空白 JSON `[agent_id,command_id,request_id]` 的 UTF-8 字节。AES 密钥由
RSA-OAEP-SHA256 包装。信封字段 `key`、`iv`、`ciphertext` 均为标准 Base64，
`ciphertext` 包含 GCM 标签。Relay 无法解密。

领取接口允许重读同一密文，客户端解密后确认领取；确认后 Relay 清除密文。客户端
保存成功后回报固定状态。断线重试沿用原请求，不重建公私钥，不重放任务。格式重填
使用新的请求 ID 和密钥。请求有效期 15 分钟，取消和过期清除密文。

短暂网络故障在仍持有运行租约时可重试。租约失效或客户端重启后，原任务中断，旧
密钥不可恢复，不自动重跑任务；用户重新发起配置。网页离线时不允许提交。

## 发布与验证

先应用 Relay 的新增表迁移并更新 Relay，再更新网页，最后更新客户端并重启。
旧客户端仍可继续原有行为；新客户端需要新 Relay 接口才能提供网页凭据入口。
网页需要 HTTPS 或 localhost，以支持 WebCrypto。

模拟测试：`uv run pytest tests/test_remote_secret.py tests/test_secret_input.py -q`。
网页使用 Node 内置测试验证 WebCrypto 与代码块渲染，Relay 单元及接口模拟测试验证
归属、状态转换、重复提交和密文删除。PostgreSQL 集成测试另需测试数据库。测试不
连接真实 CDS 服务，也不使用真实凭据。
