项目：sanic-org-sanic-5509b6cf-a51a-4de1-9164-2a5105f2f11a｜Sanic PROXY v2 前导接入与请求地址传递
复现验证：.venv/bin/pytest -q -p no:cacheprovider tests/test_proxy_protocol.py
原始结果：69 passed in 4.37s。

Rubric ID：1｜状态：通过｜期望行为：监听端口可选启用 PROXY v2，关闭时保持原连接行为｜实际观察：PROXY_PROTOCOL 默认关闭，关闭时沿 HTTP 原协议路径处理；启用时协议工厂创建 ProxyProtocol｜直接证据：sanic/config.py 默认配置；sanic/server/runners.py:271-315；tests/test_proxy_protocol.py:621-715。
Rubric ID：2｜状态：通过｜期望行为：分片前导完整验证前不进入 HTTP、WebSocket 或 TLS 处理｜实际观察：IPv4/IPv6 分段输入在前导收完整前等待，完整验证后才交接｜直接证据：sanic/server/protocols/proxy_protocol.py:77-146,275-299；tests/test_proxy_protocol.py:115-130,356-375,560-620。
Rubric ID：3｜状态：通过｜期望行为：PROXY 命令使用前导地址，LOCAL 命令保留 socket 地址｜实际观察：HTTP 响应分别显示 PROXY 声明地址和底层连接地址｜直接证据：sanic/server/protocols/proxy_protocol.py:123-151,325-352；tests/test_proxy_protocol.py:315-354。
Rubric ID：4｜状态：通过｜期望行为：前导后已到达字节无损交给 HTTP、WebSocket 或 TLS｜实际观察：同段 HTTP、TLS ClientHello 与 WebSocket 握手均完成，下游协议接收后续请求数据｜直接证据：sanic/server/protocols/proxy_protocol.py:230-254,338-376；tests/test_proxy_protocol.py:356-364,560-620。
Rubric ID：5｜状态：通过｜期望行为：畸形、超长、协议不匹配或未收全前导在请求处理前关闭连接｜实际观察：错误签名、版本、DGRAM、地址族、长度、超长与 EOF 输入均拒绝且不返回 HTTP 响应｜直接证据：sanic/server/protocols/proxy_protocol.py:77-146,300-322；tests/test_proxy_protocol.py:139-200,376-422。
Rubric ID：6｜状态：通过｜期望行为：坏前导关闭后同一 worker 继续处理后续连接｜实际观察：异常连接关闭后，下一条合法 PROXY 请求返回成功响应｜直接证据：sanic/server/protocols/proxy_protocol.py:300-322 关闭当前 transport；tests/test_proxy_protocol.py:445-462。
Rubric ID：7｜状态：通过｜期望行为：多 worker 启动后的监听连接使用相同 PROXY 配置｜实际观察：双 worker 服务返回前导客户端地址，协议工厂启用和关闭配置用例通过｜直接证据：sanic/server/runners.py:271-315 从共享 app.config.PROXY_PROTOCOL 构造协议；tests/test_proxy_protocol.py:658-775。
Rubric ID：8｜状态：通过｜期望行为：启用 PROXY 后 Forwarded 和 X-Forwarded-For 保持原语义｜实际观察：PROXY 地址解析后，X-Forwarded-For 仍按现有规则更新请求地址｜直接证据：tests/test_proxy_protocol.py:524-558；ConnInfo 地址进入现有 Request 构造路径。
