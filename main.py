"""断线私聊补发插件。

监听 NapCat 与 AstrBot 的 WebSocket 连接（lifecycle.connect），断线重连后
通过 NapCat 的 get_friend_msg_history 扩展 API 拉取断线期间的私聊历史消息，
筛选出未回复的消息并注入 AstrBot，让机器人像正常收到一样逐条回复。
"""

import asyncio
import time

from astrbot.api import logger
from astrbot.api.message_components import File, Image, Plain, Record, Video
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType


@register(
    "astrbot_plugin_offline_reply",
    "user",
    "断线重连后自动拉取并回复断线期间的私聊消息（仅 aiocqhttp/NapCat）",
    "0.1",
    "https://github.com/example/astrbot_plugin_offline_reply",
)
class OfflineReplyPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.adapter = None
        self.client = None
        self.self_id = None
        self._busy = False
        # 每次拉取的历史消息条数
        self._history_count = int(self.config.get("history_count", 20) or 20)
        # 去重：记录最近已注入的 message_id，防止离线消息被双渠道重复处理
        self._seen_msg_ids: set[str] = set()
        self._seen_max = 500
        self._find_platform()

    def _find_platform(self) -> None:
        try:
            platforms = self.context.platform_manager.platform_insts
            for platform in platforms:
                if platform.meta().name == "aiocqhttp":
                    self.adapter = platform
                    self.client = platform.get_client()
                    break
        except Exception as e:
            logger.error(f"[offline_reply] 查找 aiocqhttp 适配器失败: {e}")

        if self.client is None:
            logger.warning("[offline_reply] 未找到 aiocqhttp 适配器，插件不生效")
            return

        try:

            @self.client.on_websocket_connection
            async def _on_connect(event):
                # 每次 NapCat 重连（含首次连接）都会触发 lifecycle.connect
                asyncio.create_task(self._handle_connect(event))

        except Exception as e:
            logger.error(f"[offline_reply] 注册连接回调失败: {e}")

    async def _handle_connect(self, event) -> None:
        if self.adapter is None or self.client is None or self._busy:
            return
        self._busy = True
        try:
            now = int(time.time())
            try:
                sid = getattr(event, "self_id", None)
                if sid:
                    self.self_id = str(sid)
            except Exception:
                pass

            last_online = await self.get_kv_data("last_online", None)
            # 先记录本次在线时间，再处理
            await self.put_kv_data("last_online", now)

            if last_online is None:
                logger.info("[offline_reply] 首次连接，记录基线时间，不补发")
                return

            last_online = int(last_online)
            if now <= last_online:
                return

            uids = await self._collect_contacts()
            if not uids:
                logger.info("[offline_reply] 无最近联系人/好友可查")
                return

            total = 0
            for uid in uids:
                if uid == self.self_id:
                    continue
                msgs = await self._fetch_history(uid)
                for msg in msgs:
                    if await self._try_inject(uid, msg, last_online):
                        total += 1

            if total:
                logger.info(f"[offline_reply] 已补发 {total} 条断线期间私聊消息")
        except Exception as e:
            logger.exception(f"[offline_reply] 重连处理异常: {e}")
        finally:
            self._busy = False

    async def _collect_contacts(self) -> list[str]:
        """收集需要检查的私聊对象：优先最近联系人，退回好友列表。"""
        uids: list[str] = []
        try:
            ret = await self.client.call_action(action="get_recent_contact")
            for item in ret or []:
                uid = self._pick_uid(item)
                if uid and uid not in uids:
                    uids.append(uid)
        except Exception as e:
            logger.debug(f"[offline_reply] get_recent_contact 失败: {e}")

        if not uids:
            try:
                ret = await self.client.call_action(action="get_friend_list")
                for item in ret or []:
                    uid = self._pick_uid(item)
                    if uid and uid not in uids:
                        uids.append(uid)
            except Exception as e:
                logger.debug(f"[offline_reply] get_friend_list 失败: {e}")

        return uids

    @staticmethod
    def _pick_uid(item) -> str | None:
        if not isinstance(item, dict):
            return None
        for key in ("user_id", "peer_id", "contact_id", "uin", "friend_id"):
            value = item.get(key)
            if value is not None and str(value) != "":
                return str(value)
        return None

    async def _fetch_history(self, uid: str) -> list:
        try:
            ret = await self.client.call_action(
                action="get_friend_msg_history",
                user_id=uid,
                count=self._history_count,
            )
            if isinstance(ret, dict):
                ret = ret.get("messages") or ret.get("data") or []
            return ret if isinstance(ret, list) else []
        except Exception as e:
            logger.debug(f"[offline_reply] 拉取 {uid} 历史消息失败: {e}")
            return []

    async def _try_inject(self, uid: str, msg, last_online: int) -> bool:
        if not isinstance(msg, dict):
            return False
        try:
            t = int(msg.get("time", 0) or 0)
            if t <= last_online:
                return False

            sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
            sender_uid = str(sender.get("user_id") or msg.get("user_id") or "")
            if self.self_id and sender_uid == self.self_id:
                # 机器人自己发的消息，跳过
                return False

            msg_id = str(msg.get("message_id") or msg.get("real_id") or "")
            if msg_id:
                if msg_id in self._seen_msg_ids:
                    return False
                self._seen_msg_ids.add(msg_id)
                if len(self._seen_msg_ids) > self._seen_max:
                    # 简单裁剪，防止无限增长
                    self._seen_msg_ids = set(list(self._seen_msg_ids)[-self._seen_max // 2:])

            components = self._to_components(msg.get("message"))
            if not components:
                return False
            text = "".join(c.text for c in components if isinstance(c, Plain))

            abm = AstrBotMessage()
            abm.self_id = self.self_id or str(msg.get("self_id") or "")
            abm.sender = MessageMember(sender_uid or uid, sender.get("nickname") or sender_uid or uid)
            abm.type = MessageType.FRIEND_MESSAGE
            abm.session_id = sender_uid or uid
            abm.message_id = msg_id or str(int(time.time() * 1000))
            abm.timestamp = t
            abm.message = components
            abm.message_str = text or "[非文本消息]"
            abm.raw_message = msg

            await self.adapter.handle_msg(abm)
            return True
        except Exception as e:
            logger.error(f"[offline_reply] 注入消息失败: {e}")
            return False

    @staticmethod
    def _to_components(segments) -> list:
        """把 NapCat 的消息段转成 AstrBot 消息组件，文本/图片/语音/视频/文件都保留。"""
        if isinstance(segments, str):
            t = segments.strip()
            return [Plain(text=t)] if t else []
        if not isinstance(segments, list):
            return []

        out: list = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            st = seg.get("type")
            data = seg.get("data") or {}
            if not isinstance(data, dict):
                data = {}

            if st == "text":
                t = data.get("text") or ""
                if t:
                    out.append(Plain(text=t))
            elif st == "image":
                f = data.get("url") or data.get("file")
                if f:
                    out.append(Image(file=f))
            elif st in ("record", "voice"):
                f = data.get("url") or data.get("file")
                if f:
                    out.append(Record(file=f))
            elif st == "video":
                f = data.get("url") or data.get("file")
                if f:
                    out.append(Video(file=f))
            elif st == "file":
                f = data.get("file") or data.get("url") or ""
                if f:
                    out.append(
                        File(
                            name=data.get("name") or "file",
                            file=f,
                            url=data.get("url") or "",
                        )
                    )
            elif st == "face":
                out.append(Plain(text="[表情]"))
            elif st == "at":
                qq = data.get("qq") or ""
                if qq:
                    out.append(Plain(text=f"[@{qq}]"))
        return out

