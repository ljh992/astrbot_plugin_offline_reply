# 断线私聊补发 (astrbot_plugin_offline_reply)

AstrBot 插件：断线重连后，自动拉取断线期间收到的私聊消息并补回复。

## 解决的问题

AstrBot 掉线（重启、网络中断、NapCat 断开）期间，别人发来的私聊消息不会推给机器人，重连后就永远丢了。

这个插件监听 NapCat 与 AstrBot 的 WebSocket 连接，重连时通过 NapCat 的 `get_friend_msg_history` 拉取这段时间的历史私聊，把没处理过的消息重新注入 AstrBot，让机器人像刚收到一样正常回复。

## 效果

- 断线期间的文字、图片、语音、视频、文件、表情、@ 都会还原成对应的消息组件
- 机器人自己发的消息、断线前已经处理过的消息会被跳过
- 首次连接只记录时间基线，不会把历史消息全补一遍

## 安装

在 AstrBot WebUI 的插件市场搜索「断线私聊补发」，或手动：

```bash
cd AstrBot/data/plugins
git clone <本仓库地址> astrbot_plugin_offline_reply
```

然后在 WebUI 插件管理里启用（或重载插件）。

## 配置

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `history_count` | 每个联系人每次拉取的历史消息条数 | `20` |

## 依赖与限制

- 仅支持 `aiocqhttp`（OneBot v11 / NapCat）平台
- 依赖 NapCat 的扩展接口 `get_friend_msg_history`、`get_recent_contact`
- 能补到多少消息取决于 QQ 服务端的漫游记录，太久远的补不到
- 消息太多时补发会比较集中，`history_count` 可以调小

## 原理

1. 监听 `lifecycle.connect`，记录上次在线时间到插件 KV 存储
2. 重连时取出上次在线时间，遍历最近联系人（失败退回好友列表）
3. 拉取每个会话的历史消息，过滤出时间晚于上次在线、且不是机器人自己发的
4. 转成 `AstrBotMessage` 交给适配器的 `handle_msg`，走正常回复流程

## 许可证

MIT
