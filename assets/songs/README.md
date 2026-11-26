# 歌库 · 让她唱歌

TTS 只能"说话"，唱歌要用**歌声转换（SVC）**或歌声合成提前离线做好，存到这里，再由 `sing` 工具发出去。
这样做的好处：音质稳定、不占实时算力、想唱哪首就放哪首。

## 目录约定

```
assets/songs/
├── manifest.json
├── 晴天.silk          # 或 .wav，发送时自动转 silk
└── 小情歌.wav
```

`manifest.json`：

```json
{
  "songs": [
    { "id": "qingtian", "title": "晴天", "file": "晴天.silk", "duration": 45.2, "tags": ["周杰伦", "温柔"] },
    { "id": "xiaoqingge", "title": "小情歌", "file": "小情歌.wav", "duration": 38.0, "tags": ["苏打绿"] }
  ]
}
```

- `file` 相对本目录；`.silk` 直接发，`.wav` 会自动转 silk（24kHz 单声道 16bit 最稳）
- `duration` 秒，可省略（省略时 wav 会算，silk 建议填）
- 片段建议 **20–60 秒**，太长 QQ 语音体验差

## 怎么生成她唱的歌（三条路线）

1. **GPT-SoVITS v2Pro（推荐，和她的 TTS 音色同一套）**
   - 用她的参考音（5–60 秒干净人声）做 zero-shot / 1 分钟微调
   - 自带 SVC（歌声转换）：输入一首歌的干声 → 输出她的声音
   - 8G 显存可跑，MIT，社区教程多
2. **so-vits-svc-fork / RVC（歌声转换专用）**
   - 用 10–30 分钟她的语音训练一个小模型，把干声转成她的音色
   - so-vits-svc 原库停更，用 fork（2026 仍活跃）
3. **商业歌声合成**：ACE Studio / Synthesizer V（音质最好，需授权与费用）

> 注意：拿别人的歌做"她唱的版本"自娱自乐可以，公开发布（直播/B站投稿）涉及词曲版权与平台规则，先确认再发。

## 生成后检查

把文件放进本目录、写好 manifest，然后在聊天里说「唱首歌」，或者直接：

```bash
curl -s localhost:8000/api/agent/tools | python3 -m json.tool | grep -A2 '"sing"'
```
