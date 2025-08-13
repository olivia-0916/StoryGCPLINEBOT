import os, sys, json, re, time, uuid, random, traceback, threading
from datetime import datetime, timedelta

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage

import requests
import logging

# =============== 日誌設定 ===============
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(asctime)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
    force=True,
)
log = logging.getLogger("app")

# =============== 基礎設定 ===============
sys.stdout.reconfigure(encoding="utf-8")
app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET      = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY           = os.environ.get("OPENAI_API_KEY")
GCS_BUCKET               = os.environ.get("GCS_BUCKET", "storybotimage")
IMAGE_SIZE               = os.environ.get("IMAGE_SIZE", "512x512").strip()

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    log.error("LINE credentials missing.")
if not OPENAI_API_KEY:
    log.warning("OPENAI_API_KEY is empty; image generation will fail.")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)

log.info("🚀 app boot: public GCS URL mode (Uniform access + bucket public)")

# =============== Firebase / Firestore（容錯） ===============
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud import storage as gcs_storage
from google.api_core.exceptions import GoogleAPIError

FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS")
FIREBASE_PROJECT_ID  = os.environ.get("FIREBASE_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")

def _init_firebase():
    try:
        if firebase_admin._apps:
            return firestore.client()
        cred = None
        if FIREBASE_CREDENTIALS:
            try:
                cred = credentials.Certificate(json.loads(FIREBASE_CREDENTIALS))
                log.info("✅ Firebase: using inline service account JSON")
            except Exception as e:
                log.warning("⚠️ FIREBASE_CREDENTIALS invalid: %s", e)
        if cred is None:
            cred = credentials.ApplicationDefault()
            log.info("✅ Firebase: using Application Default Credentials")
        firebase_admin.initialize_app(cred, {'projectId': FIREBASE_PROJECT_ID} if FIREBASE_PROJECT_ID else None)
        return firestore.client()
    except Exception as e:
        log.error("❌ Firebase init failed: %s", e)
        return None

db = _init_firebase()

# =============== GCS（Uniform + 公開讀取） ===============
gcs_client = gcs_storage.Client()
gcs_bucket = gcs_client.bucket(GCS_BUCKET)

def gcs_upload_bytes(data: bytes, filename: str, content_type: str = "image/png"):
    t0 = time.time()
    try:
        blob = gcs_bucket.blob(filename)
        blob.cache_control = "public, max-age=31536000"
        blob.upload_from_string(data, content_type=content_type)
        url = f"https://storage.googleapis.com/{gcs_bucket.name}/{filename}"
        log.info("☁️ GCS upload ok | ms=%d | name=%s | bytes=%d | url=%s",
                 int((time.time()-t0)*1000), filename, len(data or b""), url)
        return url
    except GoogleAPIError as e:
        log.exception("❌ GCS API error: %s", e)
    except Exception as e:
        log.exception("❌ GCS unknown error: %s", e)
    return None

# =============== OpenAI 初始化 ===============
_openai_mode = None
_oai_client = None

def _init_openai():
    global _openai_mode, _oai_client
    try:
        from openai import OpenAI
        _oai_client = OpenAI(api_key=OPENAI_API_KEY)
        _openai_mode = "sdk1"
        log.info("✅ OpenAI init: sdk1")
    except Exception:
        import openai
        openai.api_key = OPENAI_API_KEY
        _oai_client = openai
        _openai_mode = "legacy"
        log.info("✅ OpenAI init: legacy")

_init_openai()

def openai_images_generate(prompt: str, size: str = "512x512"):
    try:
        t0 = time.time()
        log.info("🖼️ images.generate start | size=%s | prompt_len=%d", size, len(prompt))
        img_bytes = None

        if _openai_mode == "sdk1":
            resp = _oai_client.images.generate(
                model="gpt-image-1",
                prompt=prompt,
                size=size,
            )
            datum = resp.data[0]
            b64 = getattr(datum, "b64_json", None)
            if b64:
                import base64
                img_bytes = base64.b64decode(b64)
            elif getattr(datum, "url", None):
                r = requests.get(datum.url, timeout=120)
                r.raise_for_status()
                img_bytes = r.content
        else:
            resp = _oai_client.Image.create(
                model="gpt-image-1",
                prompt=prompt,
                size=size,
            )
            d0 = resp["data"][0]
            b64 = d0.get("b64_json")
            if b64:
                import base64
                img_bytes = base64.b64decode(b64)
            elif d0.get("url"):
                r = requests.get(d0["url"], timeout=120)
                r.raise_for_status()
                img_bytes = r.content

        if not img_bytes:
            log.error("💥 images.generate: no image content in response.")
            return None

        log.info("🖼️ images.generate ok | ms=%d | bytes=%d",
                 int((time.time()-t0)*1000), len(img_bytes))
        return img_bytes
    except Exception as e:
        log.exception("💥 images.generate error: %s", e)
        return None

# =============== 會話記憶 ===============
user_sessions = {}
user_seeds    = {}

def _ensure_session(user_id):
    sess = user_sessions.setdefault(user_id, {"messages": [], "paras": []})
    user_seeds.setdefault(user_id, random.randint(100000, 999999))
    return sess

def save_chat(user_id, role, text):
    if not db:
        return
    try:
        db.collection("users").document(user_id).collection("chat").add({
            "role": role, "text": text, "timestamp": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        log.warning("⚠️ save_chat failed: %s", e)

def save_story_summary(user_id, paragraphs):
    if not db:
        return
    try:
        db.collection("users").document(user_id).collection("story")\
          .document("latest_summary").set({
            "paragraphs": paragraphs, "updated_at": firestore.SERVER_TIMESTAMP
          })
    except Exception as e:
        log.warning("⚠️ save_story_summary failed: %s", e)

def load_latest_story_paragraphs(user_id):
    if not db:
        return None
    try:
        doc = db.collection("users").document(user_id).collection("story")\
               .document("latest_summary").get()
        if doc.exists:
            return (doc.to_dict().get("paragraphs") or [])[:5]
    except Exception as e:
        log.warning("⚠️ load_latest_story_paragraphs failed: %s", e)
    return None

# =============== 摘要與分段 ===============
def generate_story_summary(messages):
    sysmsg = (
        "請將以下對話整理成 5 段完整故事，每段 2–3 句（約 60–120 字），"
        "自然呈現場景、角色、主要動作與關鍵物件，不要列點外的額外說明。"
    )
    msgs = [{"role": "system", "content": sysmsg}] + messages
    try:
        if _openai_mode == "sdk1":
            resp = _oai_client.chat.completions.create(
                model="gpt-4o-mini", messages=msgs, temperature=0.5
            )
            return resp.choices[0].message.content.strip()
        else:
            resp = _oai_client.ChatCompletion.create(
                model="gpt-4o-mini", messages=msgs, temperature=0.5
            )
            return resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error("❌ OpenAI chat error: %s", e)
        return None

def extract_paragraphs(summary):
    if not summary:
        return []
    lines = [re.sub(r"^\d+\.?\s*", "", x.strip()) for x in summary.split("\n") if x.strip()]
    return lines[:5]

# =============== 圖像 Prompt ===============
BASE_STYLE = (
    "watercolor storybook illustration, warm earthy palette, soft brush textures, "
    "clean composition, child-friendly shapes, consistent character design. "
    "No text, letters, logos, watermarks, signage, or brand names."
)

def build_scene_prompt(main_desc: str, extra: str = ""):
    parts = [BASE_STYLE, main_desc]
    if extra:
        parts.append(extra)
    return " ".join(parts)

# =============== Flask routes ===============
@app.route("/")
def root():
    log.info("🏥 health check")
    return "LINE GPT Webhook is running!"

@app.route("/callback", methods=["POST"])
def callback():
    sig = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    log.info("🌐 /callback hit | sig_present=%s | len=%s", bool(sig), len(body) if body else 0)
    if not sig:
        return "OK"
    try:
        handler.handle(body, sig)
        log.info("✅ handler.handle success")
    except InvalidSignatureError:
        log.error("❌ InvalidSignatureError")
        abort(400)
    except Exception as e:
        log.exception("💥 handle error: %s", e)
        abort(500)
    return "OK"

# =============== LINE 主流程 ===============
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = (event.message.text or "").strip()
    log.info("📩 LINE text | user=%s | text=%s", user_id, text)

    reply_token = event.reply_token
    sess = _ensure_session(user_id)
    sess["messages"].append({"role": "user", "content": text})
    if len(sess["messages"]) > 60:
        sess["messages"] = sess["messages"][-60:]
    save_chat(user_id, "user", text)

    if re.search(r"(整理|總結|summary)", text):
        compact = [{"role": "user", "content": "\n".join([m["content"] for m in sess["messages"] if m["role"] == "user"][-8:])}]
        summary = generate_story_summary(compact) or "1.\n2.\n3.\n4.\n5."
        paras = extract_paragraphs(summary)
        sess["paras"] = paras
        save_story_summary(user_id, paras)
        line_bot_api.reply_message(reply_token, TextSendMessage("✨ 故事總結完成：\n" + summary))
        save_chat(user_id, "assistant", summary)
        return

    m = re.search(r"(畫|請畫|幫我畫)第([一二三四五12345])段", text)
    if m:
        n_map = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
                 '1': 1, '2': 2, '3': 3, '4': 4, '5': 5}
        idx = n_map[m.group(2)] - 1
        extra = re.sub(r"(畫|請畫|幫我畫)第[一二三四五12345]段", "", text).strip(" ，,。.!！")
        # 先回覆
        line_bot_api.reply_message(reply_token, TextSendMessage(f"收到！第 {idx+1} 段開始生成，完成後會再傳給你～"))
        # 背景生成
        threading.Thread(target=_draw_and_push, args=(user_id, idx, extra), daemon=True).start()
        return

    line_bot_api.reply_message(reply_token, TextSendMessage("我懂了！想再補充一點嗎？主角是誰？在哪裡？想發生什麼？"))
    save_chat(user_id, "assistant", "引導")

@handler.add(MessageEvent)
def handle_non_text(event):
    user_id = getattr(event.source, "user_id", "unknown")
    etype = type(event.message).__name__
    log.info("🧾 LINE non-text | user=%s | type=%s", user_id, etype)
    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage("目前我只看得懂文字訊息喔～"))
    except Exception:
        pass

# =============== 背景生成並 push ===============
def _get_paragraphs_for_user(user_id, sess):
    return load_latest_story_paragraphs(user_id) or sess.get("paras") or []

def _draw_and_push(user_id, idx, extra):
    try:
        log.info("🎯 [bg] draw request | user=%s | idx=%d | extra=%s", user_id, idx, extra)
        sess = _ensure_session(user_id)
        paras = _get_paragraphs_for_user(user_id, sess)
        if not paras or idx >= len(paras):
            line_bot_api.push_message(user_id, TextSendMessage("我需要再多一點故事內容，才能開始畫喔～"))
            return
        scene = paras[idx]
        prompt = build_scene_prompt(f"Scene: {scene}", extra)
        img_bytes = openai_images_generate(prompt, size=IMAGE_SIZE)
        if not img_bytes:
            line_bot_api.push_message(user_id, TextSendMessage("圖片生成暫時失敗了，稍後再試一次可以嗎？"))
            return
        fname = f"line_images/{user_id}-{uuid.uuid4().hex[:6]}_s{idx+1}.png"
        public_url = gcs_upload_bytes(img_bytes, fname, "image/png")
        if not public_url:
            line_bot_api.push_message(user_id, TextSendMessage("上傳圖片時出了點狀況，等等再請我重畫一次～"))
            return
        msgs = [
            TextSendMessage(f"第 {idx+1} 段完成了！（{IMAGE_SIZE}）"),
            ImageSendMessage(public_url, public_url),
        ]
        line_bot_api.push_message(user_id, msgs)
    except Exception as e:
        log.exception("💥 [bg] draw fail: %s", e)
        try:
            line_bot_api.push_message(user_id, TextSendMessage("生成中遇到小狀況，等等再試一次可以嗎？"))
        except Exception:
            pass

# =============== 啟動 ===============
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
