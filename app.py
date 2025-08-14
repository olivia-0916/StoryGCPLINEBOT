import os, sys, json, re, time, uuid, random, traceback, threading
from datetime import datetime
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
sys.stdout.reconfigure(encoding="utf-8")

# =============== 基礎設定 ===============
app = Flask(__name__)
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET       = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY            = os.environ.get("OPENAI_API_KEY")
GCS_BUCKET                = os.environ.get("GCS_BUCKET", "storybotimage")
IMAGE_SIZE_ENV            = (os.environ.get("IMAGE_SIZE") or "1024x1024").strip()

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    log.error("LINE credentials missing.")
if not OPENAI_API_KEY:
    log.warning("OPENAI_API_KEY is empty; image generation will fail.")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler       = WebhookHandler(LINE_CHANNEL_SECRET)
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

ALLOWED_SIZES = {"1024x1024", "1024x1536", "1536x1024", "auto"}

def _normalize_size(size: str) -> str:
    size = (size or "").strip()
    if size not in ALLOWED_SIZES:
        log.warning("⚠️ IMAGE_SIZE=%s not supported; fallback -> 1024x1024", size)
        return "1024x1024"
    return size

def openai_images_generate(prompt: str, size: str):
    size = _normalize_size(size)
    try:
        t0 = time.time()
        log.info("🖼️ images.generate start | size=%s | prompt_len=%d", size, len(prompt))
        img_bytes = None

        if _openai_mode == "sdk1":
            resp = _oai_client.images.generate(
                model="dall-e-3", # 建議使用 DALL-E 3，效果更佳
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
                model="dall-e-3", # 建議使用 DALL-E 3，效果更佳
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

# =============== 會話記憶（含角色卡） ===============
# 修改：將單一角色卡改為多個角色卡的字典
user_sessions = {}  # {uid: {"messages": [...], "paras": [...], "characters": {...}, "story_id": "..."}}
user_seeds    = {}

def _ensure_session(user_id):
    # 修改：初始化時使用 'characters' 而非 'character'
    sess = user_sessions.setdefault(user_id, {"messages": [], "paras": [], "characters": {}, "story_id": None})
    user_seeds.setdefault(user_id, random.randint(100000, 999999))
    if sess.get("story_id") is None:
        sess["story_id"] = f"story-{int(time.time())}-{random.randint(1000,9999)}"
    return sess

def save_chat(user_id, role, text):
    if not db: return
    try:
        db.collection("users").document(user_id).collection("chat").add({
            "role": role, "text": text, "timestamp": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        log.warning("⚠️ save_chat failed: %s", e)

def save_current_story(user_id, sess):
    if not db: return
    try:
        doc = {
            "story_id": sess.get("story_id"),
            "paragraphs": sess.get("paras", []),
            "character_cards": sess.get("characters", {}), # 修改：儲存多個角色卡
            "updated_at": firestore.SERVER_TIMESTAMP
        }
        db.collection("users").document(user_id).collection("story").document("current").set(doc)
    except Exception as e:
        log.warning("⚠️ save_current_story failed: %s", e)

def load_current_story(user_id, sess):
    if not db: return
    try:
        doc = db.collection("users").document(user_id).collection("story").document("current").get()
        if doc.exists:
            d = doc.to_dict() or {}
            sess["story_id"] = d.get("story_id") or sess.get("story_id")
            sess["paras"]    = d.get("paragraphs") or sess.get("paras", [])
            sess["characters"]= d.get("character_cards") or sess.get("characters", {}) # 修改：讀取多個角色卡
    except Exception as e:
        log.warning("⚠️ load_current_story failed: %s", e)

# =============== 角色卡抽取（中文規則） ===============
# 顏色映射（中 -> 英，供提示更穩定）
COLOR_MAP = {
    "紫色":"purple","紫":"purple","黃色":"yellow","黃":"yellow","紅色":"red","紅":"red","藍色":"blue","藍":"blue",
    "綠色":"green","綠":"green","黑色":"black","黑":"black","白色":"white","白":"white","粉紅色":"pink","粉紅":"pink","粉":"pink",
    "橘色":"orange","橘":"orange","棕色":"brown","棕":"brown","咖啡色":"brown","咖啡":"brown","灰色":"gray","灰":"gray"
}
TOP_WORDS = r"(上衣|衣服|襯衫|T恤|T-shirt|外套|毛衣|連帽衣|風衣|裙子|長裙|洋裝)" # 增加更多衣物詞彙
HAIR_STYLE_WORDS = r"(長髮|短髮|直髮|捲髮|波浪|馬尾|雙馬尾|辮子)"
GENDER_WORDS = r"(男孩|女孩|男性|女性|男生|女生|哥哥|姊姊|弟弟|妹妹|叔叔|阿姨|爸爸|媽媽)"

# 新增：常用角色名稱列表
CHARACTER_NAMES = ["小明", "小芳", "傑克", "瑪莉", "主角", "我"] # 可根據需求擴充

def _find_character_name(text):
    for name in CHARACTER_NAMES:
        if name in text:
            return name
    return None

def maybe_update_character_card(sess, user_id, text):
    """
    從使用者本輪訊息中抽取外觀線索並更新 sess['characters']；同步寫入 Firestore current。
    針對常見特徵做規則抽取：上衣顏色/種類、頭髮顏色/長短/眼鏡/帽子/性別線索。
    """
    # 預設更新「主角」的角色卡
    char_name = _find_character_name(text) or "主角"
    
    updated = False
    # 修改：從 'characters' 字典中獲取或創建特定角色的卡片
    card = sess["characters"].setdefault(char_name, {})

    # 1) 上衣/外套 + 顏色
    if re.search(TOP_WORDS, text):
        zh, en = _find_color(text)
        if zh:
            card["top_color_zh"] = zh
            card["top_color_en"] = en
            updated = True
        m_top = re.search(TOP_WORDS, text)
        if m_top:
            card["top_type_zh"] = m_top.group(1)
            updated = True

    # 2) 頭髮顏色/長短
    if "髮" in text or "頭髮" in text:
        zh, en = _find_color(text)
        if zh:
            card["hair_color_zh"] = zh
            card["hair_color_en"] = en
            updated = True
        m_style = re.search(HAIR_STYLE_WORDS, text)
        if m_style:
            card["hair_style_zh"] = m_style.group(1)
            updated = True

    # 3) 眼鏡 / 帽子 / 鬍子
    if re.search(r"(戴|配).*(眼鏡)", text):
        card["accessory_glasses"] = True
        updated = True
    if re.search(r"(戴|戴著).*(帽|帽子)", text):
        card["accessory_hat"] = True
        updated = True
    if re.search(r"(留鬍|有鬍|鬍子)", text):
        card["has_beard"] = True
        updated = True

    # 4) 性別/年齡線索（僅做弱提示）
    if re.search(GENDER_WORDS, text):
        card["gender_hint_zh"] = re.search(GENDER_WORDS, text).group(1)
        updated = True

    if updated:
        log.info("🧬 character_card updated | user=%s | char=%s | card=%s", user_id, char_name, json.dumps(card, ensure_ascii=False))
        save_current_story(user_id, sess)

def render_character_card_as_text(characters: dict) -> str:
    """
    將多個角色卡渲染為可讀的提示，放進圖像 prompt。
    為每個角色單獨描述，並加入一致性提示。
    """
    if not characters: return ""
    
    all_char_zh = []
    all_char_en = []
    
    for char_name, card in characters.items():
        # 填充預設值以防遺漏
        card.setdefault("top_type_zh", "上衣")
        card.setdefault("hair_style_zh", "頭髮")
        
        parts_zh = [f"{char_name}"]
        parts_en = [f"{char_name}"]

        # 處理中文描述
        if card.get("gender_hint_zh"):
            parts_zh.append(f"是{card['gender_hint_zh']}")
        
        # 盡量讓描述流暢
        clothing_desc = ""
        if card.get("top_color_zh"):
            clothing_desc += f"穿著{card['top_color_zh']}"
        clothing_desc += f"{card['top_type_zh']}" if card.get("top_type_zh") else ""
        if clothing_desc: parts_zh.append(clothing_desc)
        
        hair_desc = ""
        if card.get("hair_color_zh"):
            hair_desc += f"{card['hair_color_zh']}"
        if card.get("hair_style_zh"):
            hair_desc += f"{card['hair_style_zh']}"
        if hair_desc: parts_zh.append(f"有{hair_desc}")
            
        accessories_desc = []
        if card.get("accessory_glasses"): accessories_desc.append("戴眼鏡")
        if card.get("accessory_hat"): accessories_desc.append("戴帽子")
        if card.get("has_beard"): accessories_desc.append("留鬍子")
        if accessories_desc: parts_zh.append("，".join(accessories_desc))
            
        all_char_zh.append("".join(parts_zh))

        # 處理英文描述
        if card.get("top_color_en") or card.get("top_type_zh"):
            parts_en.append(f"wears a {card.get('top_color_en','')} {card.get('top_type_zh','top')}")
        if card.get("hair_color_en"):
            parts_en.append(f"has {card['hair_color_en']} hair")
        if card.get("hair_style_zh"):
            parts_en.append(card["hair_style_zh"])
        if card.get("accessory_glasses"):
            parts_en.append("wears glasses")
        if card.get("accessory_hat"):
            parts_en.append("wears a hat")
        if card.get("has_beard"):
            parts_en.append("has a beard")
        
        all_char_en.append(f"{char_name}: " + ", ".join(parts_en))

    zh_line = "、".join(all_char_zh) + "。"
    en_line = " | ".join(all_char_en) + ". Keep character appearances consistent across scenes."

    out = []
    if zh_line and zh_line != "主角。": out.append(f"角色特徵：{zh_line}")
    if en_line: out.append(en_line)
    return " ".join(out)

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
    if not summary: return []
    lines = [re.sub(r"^\d+\.?\s*", "", x.strip()) for x in summary.split("\n") if x.strip()]
    return lines[:5]

# =============== 圖像 Prompt ===============
BASE_STYLE = (
    "watercolor storybook illustration, warm earthy palette, soft brush textures, "
    "clean composition, child-friendly shapes, consistent character design. "
    "No text, letters, logos, watermarks, signage, or brand names."
)

def build_scene_prompt(scene_desc: str, char_hint: str = "", extra: str = ""):
    parts = [BASE_STYLE, f"Scene: {scene_desc}"]
    if char_hint: parts.append(char_hint)
    if extra:      parts.append(extra)
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

    sess = _ensure_session(user_id)
    load_current_story(user_id, sess)  # 取回可能已有的 current
    sess["messages"].append({"role": "user", "content": text})
    if len(sess["messages"]) > 60:
        sess["messages"] = sess["messages"][-60:]
    save_chat(user_id, "user", text)

    # 先嘗試從本輪訊息抽取角色卡線索（即時更新）
    maybe_update_character_card(sess, user_id, text)

    reply_token = event.reply_token

    # 整理/總結 -> 建立新故事、重置角色卡
    if re.search(r"(整理|總結|summary)", text):
        compact = [{"role": "user", "content": "\n".join([m["content"] for m in sess["messages"] if m["role"] == "user"][-8:])}]
        summary = generate_story_summary(compact) or "1.\n2.\n3.\n4.\n5."
        paras = extract_paragraphs(summary)
        sess["paras"] = paras
        sess["story_id"] = f"story-{int(time.time())}-{random.randint(1000,9999)}"
        sess["characters"] = {}  # 新故事重置角色卡
        save_current_story(user_id, sess)
        line_bot_api.reply_message(reply_token, TextSendMessage("✨ 故事總結完成：\n" + summary))
        save_chat(user_id, "assistant", summary)
        return

    # 畫第N段
    m = re.search(r"(畫|請畫|幫我畫)第([一二三四五12345])段", text)
    if m:
        n_map = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
                 '1': 1, '2': 2, '3': 3, '4': 4, '5': 5}
        idx = n_map[m.group(2)] - 1
        extra = re.sub(r"(畫|請畫|幫我畫)第[一二三四五12345]段", "", text).strip(" ，,。.!！")
        line_bot_api.reply_message(reply_token, TextSendMessage(f"收到！第 {idx+1} 段開始生成，完成後會再傳給你～"))
        threading.Thread(target=_draw_and_push, args=(user_id, idx, extra), daemon=True).start()
        return

    # 引導
    line_bot_api.reply_message(reply_token, TextSendMessage("我懂了！想再補充一點嗎？主角長相/服裝/道具想怎麼設定？"))
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
def _get_paragraphs_for_user(sess):
    return sess.get("paras") or []

def _draw_and_push(user_id, idx, extra):
    try:
        sess = _ensure_session(user_id)
        load_current_story(user_id, sess)
        log.info("🎯 [bg] draw request | user=%s | idx=%d | extra=%s | story_id=%s", user_id, idx, extra, sess.get("story_id"))

        paras = _get_paragraphs_for_user(sess)
        if not paras or idx >= len(paras):
            line_bot_api.push_message(user_id, TextSendMessage("我需要再多一點故事內容，才能開始畫喔～"))
            return

        scene = paras[idx]
        # 修改：將多個角色卡傳入生成提示
        char_hint = render_character_card_as_text(sess.get("characters", {}))
        prompt = build_scene_prompt(scene_desc=scene, char_hint=char_hint, extra=extra)
        log.info("🧩 [bg] prompt head: %s", prompt[:200])

        size = _normalize_size(IMAGE_SIZE_ENV)
        img_bytes = openai_images_generate(prompt, size=size)
        if not img_bytes:
            line_bot_api.push_message(user_id, TextSendMessage("圖片生成暫時失敗了，稍後再試一次可以嗎？"))
            return

        fname = f"line_images/{user_id}-{uuid.uuid4().hex[:6]}_s{idx+1}.png"
        public_url = gcs_upload_bytes(img_bytes, fname, "image/png")
        if not public_url:
            line_bot_api.push_message(user_id, TextSendMessage("上傳圖片時出了點狀況，等等再請我重畫一次～"))
            return

        msgs = [
            TextSendMessage(f"第 {idx+1} 段完成了！（{size}）"),
            ImageSendMessage(public_url, public_url),
        ]
        line_bot_api.push_message(user_id, msgs)
        log.info("✅ [bg] push image sent | user=%s | url=%s", user_id, public_url)

        save_chat(user_id, "assistant", f"[image]{public_url}")

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
