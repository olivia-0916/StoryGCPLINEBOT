# app.py — LINE 故事繪本機器人
# - OpenAI 相容式導入（避免不同 1.x 小版造成匯入錯誤）
# - gpt-image-1 完整錯誤輸出（403/安全攔截等）
# - GCS 只用 V4 簽名網址（相容 Uniform bucket-level access / PAP）
# - Slot 抽取與欄位填充：只追問缺的資訊，避免重複提問
# - 故事整理切 5 段 + 隱藏參考圖 + 角色一致性

import os, sys, json, re, uuid, time, threading, traceback, random, base64, requests
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

# ---------- 基礎 ----------
sys.stdout.reconfigure(encoding="utf-8")
print("🚀 app boot: signed-url mode active, no make_public()")

# ---------- Flask / LINE ----------
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage

# ---------- OpenAI 1.x（相容式導入） ----------
import openai as _openai_mod

OpenAI = _openai_mod.OpenAI  # 客戶端

def _pick(*names, default=Exception):
    """從 openai 模組或 openai._exceptions 里拿例外類別；取不到回傳 default。"""
    for n in names:
        obj = getattr(_openai_mod, n, None)
        if obj:
            return obj
    try:
        exc_mod = __import__("openai._exceptions", fromlist=["*"])
        for n in names:
            obj = getattr(exc_mod, n, None)
            if obj:
                return obj
    except Exception:
        pass
    return default

APIStatusError        = _pick("APIStatusError", "APIError")
APIConnectionError    = _pick("APIConnectionError")
RateLimitError        = _pick("RateLimitError")
AuthenticationError   = _pick("AuthenticationError")
BadRequestError       = _pick("BadRequestError")
PermissionDeniedError = _pick("PermissionDeniedError")

# ---------- Firebase / Firestore / GCS ----------
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud import storage as gcs

# ================== 設定 ==================
app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET      = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY           = os.environ.get("OPENAI_API_KEY")
FIREBASE_CREDENTIALS     = os.environ.get("FIREBASE_CREDENTIALS")  # JSON 字串
GCS_BUCKET               = os.environ.get("GCS_BUCKET", "storybotimage")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)
client       = OpenAI(api_key=OPENAI_API_KEY)

def _firebase_creds():
    return credentials.Certificate(json.loads(FIREBASE_CREDENTIALS))
if not firebase_admin._apps:
    firebase_admin.initialize_app(_firebase_creds())
db         = firestore.client()
gcs_client = gcs.Client()
bucket     = gcs_client.bucket(GCS_BUCKET)

# ================== 狀態 / 模板 ==================
GEN_SEMAPHORE = threading.Semaphore(2)
user_sessions = {}  # {uid: {"messages":[...], "story_id": "..."}}

STYLE_PROMPT = (
    "watercolor storybook illustration, warm earthy palette, soft brush textures, "
    "clean composition, child-friendly shapes, consistent character design"
)
CONSISTENCY_GUARD = (
    "Keep the same character identity across images: same face shape, hairstyle, outfit, color palette; "
    "subtle variations only (~25%)."
)
SAFE_HEADSHOT_EXTRA = (
    "Neutral head-and-shoulders portrait, soft lighting, plain background. "
    "No text, letters, logos, watermarks, signage, or brand names."
)

SLOT_KEYS = ["character", "appearance", "location", "time", "goal", "conflict", "resolution", "tone"]
MANDATORY_SLOTS = ["character", "location", "goal"]  # 最少需要

# ================== 通用工具 ==================
def gcs_upload_bytes(data: bytes, filename: str, content_type="image/png") -> str:
    """上傳到 GCS；永遠回傳 V4 簽名網址（相容 Uniform / Public Access Prevention）"""
    blob = bucket.blob(f"line_images/{filename}")
    blob.upload_from_string(data, content_type=content_type)

    ttl_days = int(os.environ.get("GCS_SIGNED_URL_DAYS", "14"))
    url = blob.generate_signed_url(
        version="v4",
        expiration=datetime.utcnow() + timedelta(days=ttl_days),
        method="GET",
        response_disposition=f'inline; filename="{filename}"',
        content_type=content_type,
    )
    print("✅ GCS uploaded (signed URL):", url)
    return url

def save_story(story_id: str, data: dict):
    db.collection("stories").document(story_id).set(data, merge=True)

def read_story(story_id: str) -> Optional[dict]:
    doc = db.collection("stories").document(story_id).get()
    return doc.to_dict() if doc.exists else None

def save_scene(story_id: str, idx: int, data: dict):
    db.collection("stories").document(story_id).collection("scenes").document(str(idx)).set(data, merge=True)

def read_scene(story_id: str, idx: int) -> Optional[dict]:
    d = db.collection("stories").document(story_id).collection("scenes").document(str(idx)).get()
    return d.to_dict() if d.exists else None

def read_prev_image_url(story_id: str, idx: int) -> Optional[str]:
    if idx <= 1: return None
    prev = read_scene(story_id, idx-1)
    return prev.get("image_url") if prev else None

def save_chat(user_id, role, text):
    try:
        db.collection("users").document(user_id).collection("chat").add({
            "role": role, "text": text, "timestamp": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        print("⚠️ save_chat 失敗:", e)

# ================== LLM 助手 ==================
def llm_chat(messages: List[Dict[str, Any]], temperature=0.2, model="gpt-4o-mini") -> str:
    try:
        resp = client.chat.completions.create(model=model, messages=messages, temperature=temperature)
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print("❌ llm_chat error:", repr(e))
        return ""

def split_into_five_scenes(story_text: str) -> List[str]:
    msgs = [
        {"role":"system","content":"Segment the story into exactly 5 concise scene descriptions (1–2 sentences each). Return a plain 5-line list."},
        {"role":"user","content": story_text}
    ]
    raw = llm_chat(msgs, temperature=0.2)
    scenes = [re.sub(r"^[\-\•\d\.\s]+","", s).strip() for s in raw.splitlines() if s.strip()]
    scenes = (scenes + ["(empty scene)"]*5)[:5] if len(scenes) < 5 else scenes[:5]
    print("🧩 scenes:", scenes)
    return scenes

def extract_features_from_text(story_text: str) -> dict:
    msgs = [
        {"role":"system","content":"Extract concise reusable character descriptors as JSON keys: gender, age, hair, outfit, accessory, face, vibe. Infer neutral values if missing."},
        {"role":"user","content": story_text}
    ]
    out = llm_chat(msgs, temperature=0.2)
    try:
        data = json.loads(out)
    except Exception:
        data = {}
    data.setdefault("gender","female")
    data.setdefault("age","30s-40s")
    data.setdefault("hair","shoulder-length dark brown hair")
    data.setdefault("outfit","pink long dress")
    data.setdefault("face","gentle smile, round face")
    data.setdefault("vibe","warm, capable, kind")
    print("🎭 features:", data)
    return data

def refine_features_with_image(image_url: str, curr: dict) -> dict:
    msgs = [
        {"role":"system","content":"Refine character descriptors from the image. Keep keys: gender, age, hair, outfit, accessory, face, vibe. Return pure JSON."},
        {"role":"user","content":[
            {"type":"text","text":"Return JSON only."},
            {"type":"image_url","image_url":{"url": image_url}}
        ]}
    ]
    out = llm_chat(msgs, temperature=0.2)
    try:
        upd = json.loads(out)
        curr.update({k:v for k,v in upd.items() if v})
        print("🪞 refined features:", curr)
    except Exception:
        pass
    return curr

def build_prompt(scene_desc: str, features: dict, extra: str = "") -> str:
    role_bits = [features.get(k) for k in ["gender","age","hair","outfit","accessory","face","vibe"] if features.get(k)]
    role_str  = ", ".join(role_bits)
    return f"{STYLE_PROMPT}. Main character: {role_str}. {CONSISTENCY_GUARD} {extra} Scene: {scene_desc}"

# ================== OpenAI 圖片（完整錯誤輸出） ==================
def _decode_image_response(resp) -> bytes:
    return base64.b64decode(resp.data[0].b64_json)

def _print_api_error(prefix: str, err: Exception):
    print(f"{prefix}: {err.__class__.__name__}")
    if isinstance(err, APIStatusError):
        print("  status_code:", getattr(err, "status_code", None))
        msg = getattr(err, "message", "")
        if msg: print("  message:", msg)
        try:
            body = err.response.text
            if body:
                print("  response body:", body[:2048])
        except Exception:
            pass
    else:
        print("  detail:", repr(err))

def openai_generate(prompt: str, size="1024x1024", retries=1) -> bytes:
    last_err = None
    for attempt in range(retries+1):
        try:
            print("🖼️ images.generate prompt:", prompt[:600])
            r = client.images.generate(model="gpt-image-1", prompt=prompt, size=size)
            return _decode_image_response(r)
        except (APIConnectionError, RateLimitError) as e:
            last_err = e
            wait = 1.5 * (attempt + 1)
            print(f"🌐 transient error {e.__class__.__name__}, retry in {wait}s")
            time.sleep(wait)
        except (BadRequestError, APIStatusError, AuthenticationError, PermissionDeniedError) as e:
            _print_api_error("💥 images.generate error", e)
            if isinstance(e, APIStatusError) and getattr(e, "status_code", None) == 403:
                body = ""
                try:
                    body = e.response.text or ""
                except Exception:
                    pass
                if "must be verified" in body:
                    raise RuntimeError("OPENAI_ORG_NOT_VERIFIED")
            raise
        except Exception as e:
            last_err = e
            _print_api_error("💥 images.generate unknown", e)
            raise
    raise last_err

def openai_img2img(prompt: str, ref_bytes: bytes, size="1024x1024", retries=1) -> bytes:
    for attempt in range(retries+1):
        try:
            print("🖼️ images.edits prompt:", prompt[:600], "| ref_bytes:", len(ref_bytes))
            r = client.images.edits(
                model="gpt-image-1",
                prompt=prompt,
                image=[("image", ref_bytes, "ref.png")],
                size=size,
            )
            return _decode_image_response(r)
        except (APIConnectionError, RateLimitError) as e:
            wait = 1.5 * (attempt + 1)
            print(f"🌐 transient error {e.__class__.__name__}, retry in {wait}s")
            time.sleep(wait)
        except (BadRequestError, APIStatusError, AuthenticationError, PermissionDeniedError) as e:
            _print_api_error("💥 images.edits error", e)
            if isinstance(e, APIStatusError) and getattr(e, "status_code", None) == 403:
                body = ""
                try:
                    body = e.response.text or ""
                except Exception:
                    pass
                if "must be verified" in body:
                    raise RuntimeError("OPENAI_ORG_NOT_VERIFIED")
            raise

# ================== Slot 抽取與欄位填充 ==================
SLOT_KEYS = ["character", "appearance", "location", "time", "goal", "conflict", "resolution", "tone"]
MANDATORY_SLOTS = ["character", "location", "goal"]

def rule_extract_slots(text: str) -> Dict[str, str]:
    slots = {}
    # 角色/外觀
    m = re.search(r"(叫|名為|名字是|他是|她是)([^，。！？,.]{1,12})", text)
    if m: slots["character"] = m.group(2).strip()
    if re.search(r"(短髮|長髮|棕髮|黑髮|金髮|瀏海|馬尾|眼鏡|帽子)", text):
        slots["appearance"] = (slots.get("appearance","") + " " + re.findall(r"(短髮|長髮|棕髮|黑髮|金髮|瀏海|馬尾|眼鏡|帽子)", text)[0]).strip()
    # 場景
    loc_kw = re.findall(r"(在|來到|位於)([^。！!？\n]{2,12})(?:[。！!？\n]|$)", text)
    if loc_kw:
        slots["location"] = re.sub(r"^(在|來到|位於)", "", loc_kw[0][0]+loc_kw[0][1]).strip()
    # 時間
    if re.search(r"(早上|上午|中午|下午|傍晚|晚上|深夜|黎明|清晨|黃昏)", text):
        slots["time"] = re.findall(r"(早上|上午|中午|下午|傍晚|晚上|深夜|黎明|清晨|黃昏)", text)[0]
    # 目標
    m = re.search(r"(想要|希望|目標|為了|打算)([^。！!？\n]{2,20})", text)
    if m: slots["goal"] = m.group(2).strip()
    # 衝突
    m = re.search(r"(遇到|面臨|困難|挑戰|危機|阻礙)([^。！!？\n]{2,20})", text)
    if m: slots["conflict"] = m.group(2).strip()
    # 結局
    m = re.search(r"(最後|終於|結果|因此)([^。！!？\n]{2,20})", text)
    if m: slots["resolution"] = m.group(2).strip()
    # 語氣
    if re.search(r"(溫馨|緊張|感動|歡樂|神秘|冒險|療癒|寫實|童趣)", text):
        slots["tone"] = re.findall(r"(溫馨|緊張|感動|歡樂|神秘|冒險|療癒|寫實|童趣)", text)[0]
    return slots

def llm_extract_slots(text: str) -> Dict[str, str]:
    sysmsg = (
        "Extract story slots from Chinese text and return strict JSON with keys: "
        "character, appearance, location, time, goal, conflict, resolution, tone. "
        "Values should be short phrases (<=12 Chinese characters). Missing keys should be empty strings."
    )
    out = llm_chat(
        [{"role":"system","content":sysmsg},
         {"role":"user","content":text}],
        temperature=0.1
    )
    try:
        data = json.loads(out)
        return {k:(data.get(k) or "").strip() for k in SLOT_KEYS}
    except Exception:
        return {}

def merge_slots(old: Dict[str,str], new: Dict[str,str]) -> Dict[str,str]:
    out = dict(old or {})
    for k in SLOT_KEYS:
        v = (new or {}).get(k)
        if v and (k not in out or not out[k]):  # 只填補空白欄位
            out[k] = v
    return out

def format_missing_questions(slots: Dict[str,str]) -> str:
    missing = [k for k in MANDATORY_SLOTS if not slots.get(k)]
    qmap = {
        "character":"主角是誰？外觀如何？",
        "location":"故事在哪裡發生？",
        "goal":"主角的目標是什麼？",
        "conflict":"遇到什麼挑戰？",
        "time":"大概發生在什麼時間？（早上/晚上…）",
        "resolution":"最後怎麼收尾？",
        "tone":"整體氛圍想要偏向？（溫馨/冒險…）",
    }
    asks = [qmap[m] for m in missing[:2]]
    if asks:
        return "我先記下了！\n" + " / ".join(asks)
    return "很好！要我把故事整理成 5 段嗎？直接回「整理」即可。"

def slots_to_story_text(slots: Dict[str,str]) -> str:
    parts = []
    c = slots.get("character"); a=slots.get("appearance"); loc=slots.get("location")
    t = slots.get("time"); g=slots.get("goal"); con=slots.get("conflict")
    r = slots.get("resolution"); tone=slots.get("tone")
    if c and a: parts.append(f"{c}，{a}。")
    elif c: parts.append(f"{c}。")
    if loc or t: parts.append(f"故事發生在{t or ''}{loc or ''}。")
    if g: parts.append(f"他/她想要{g}。")
    if con: parts.append(f"途中遇到{con}。")
    if r: parts.append(f"最後{r}。")
    if tone: parts.append(f"整體氛圍偏{tone}。")
    return "".join(parts)

# ================== 隱藏參考圖（含降級） ==================
def ensure_hidden_reference(story_id: str):
    story = read_story(story_id) or {}
    feats = story.get("character_features")
    href  = story.get("hidden_reference_image_url")
    if feats and href:
        return
    slots = (story.get("slots") or {})
    base_text = story.get("story_text","") or slots_to_story_text(slots)
    if not feats:
        feats = extract_features_from_text(base_text)
        save_story(story_id, {"character_features": feats})

    headshot_prompt = build_prompt(
        "Head-and-shoulders portrait, neutral expression, facing camera.",
        feats,
        extra=SAFE_HEADSHOT_EXTRA
    )
    try:
        img = openai_generate(headshot_prompt)
        url = gcs_upload_bytes(img, f"{story_id}_hidden_ref.png")
        feats = refine_features_with_image(url, feats)
        save_story(story_id, {"character_features": feats, "hidden_reference_image_url": url})
    except Exception as e:
        print("⚠️ hidden reference failed, continue without it:", repr(e))
        save_story(story_id, {"hidden_reference_image_url": None})

# ================== 生成場景圖 ==================
def generate_scene_image(story_id: str, idx: int, extra: str="") -> str:
    story  = read_story(story_id) or {}
    scenes = story.get("scenes_text") or []
    if not scenes or idx < 1 or idx > 5:
        raise ValueError("Scenes not ready or index out of range.")

    base_text = story.get("story_text","") or slots_to_story_text(story.get("slots") or {})
    feats = story.get("character_features") or extract_features_from_text(base_text)
    save_story(story_id, {"character_features": feats})

    try:
        ensure_hidden_reference(story_id)
    except Exception as e:
        print("⚠️ ensure_hidden_reference error:", repr(e))

    scene_text = scenes[idx-1]
    prompt     = build_prompt(scene_text, feats, extra=extra)
    print(f"📝 scene[{idx}] prompt => {prompt}")

    ref_url = read_prev_image_url(story_id, idx) or (read_story(story_id) or {}).get("hidden_reference_image_url")

    try:
        if ref_url:
            rb  = requests.get(ref_url, timeout=30).content
            img = openai_img2img(prompt, rb)
        else:
            img = openai_generate(prompt)
    except RuntimeError as e:
        if str(e) == "OPENAI_ORG_NOT_VERIFIED":
            raise RuntimeError("OPENAI_ORG_NOT_VERIFIED")
        else:
            raise
    except APIStatusError as e:
        print("↩️ fallback to safer prompt due to APIStatusError")
        safer = prompt + " Avoid showing specific logos, school names, medical settings, or explicit content."
        img = openai_generate(safer)

    url = gcs_upload_bytes(img, f"{story_id}_s{idx}.png")
    save_scene(story_id, idx, {"text": scene_text, "prompt": prompt, "image_url": url})
    return url

# ================== 故事整理 / 對話 ==================
def compact_story_from_dialog(messages: List[Dict[str, Any]]) -> str:
    user_lines = [m["content"] for m in messages if m.get("role")=="user"]
    return "\n".join(user_lines[-12:]).strip()

def summarize_and_store(user_id: str, story_id: str, story_text: str, slots: Dict[str,str]) -> List[str]:
    base = slots_to_story_text(slots)
    corpus = (base + "\n" + story_text).strip() if story_text else base
    scenes = split_into_five_scenes(corpus)
    save_story(story_id, {
        "user_id": user_id,
        "story_text": corpus,
        "slots": slots,
        "scenes_text": scenes,
        "style_preset": "watercolor_storybook_v1",
        "updated_at": firestore.SERVER_TIMESTAMP
    })
    return scenes

def chinese_index_to_int(s: str) -> int:
    m = re.search(r"[一二三四五12345]", s)
    if not m: return -1
    mp = {'一':1,'二':2,'三':3,'四':4,'五':5,'1':1,'2':2,'3':3,'4':4,'5':5}
    return mp[m.group(0)]

# ================== 路由 ==================
@app.route("/")
def root():
    return "LINE story image bot is running."

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    if not signature:
        print("⚠️ Missing X-Line-Signature, ignore.")
        return "OK"
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ================== 主事件處理 ==================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text    = (event.message.text or "").strip()
    reply_token = event.reply_token
    print(f"📩 {user_id}: {text}")

    sess = user_sessions.setdefault(user_id, {"messages": [], "story_id": None})
    sess["messages"].append({"role":"user","content":text})
    if len(sess["messages"]) > 60: sess["messages"] = sess["messages"][-60:]
    save_chat(user_id, "user", text)

    try:
        # 1) 開始說故事
        if re.search(r"(開始說故事|說故事|講個故事|開始創作|我們來講故事吧)", text):
            story_id = f"{user_id}-{uuid.uuid4().hex[:6]}"
            sess["story_id"] = story_id
            save_story(story_id, {
                "user_id": user_id,
                "created_at": firestore.SERVER_TIMESTAMP,
                "slots": {}
            })
            line_bot_api.reply_message(reply_token, TextSendMessage("好的！自由描述你的故事。\n給完要素後，跟我說「整理」我會切成 5 段。"))
            return

        # 2) 整理 → 分 5 段
        if re.search(r"(整理|總結|summary)", text):
            story_id = sess.get("story_id") or f"{user_id}-{uuid.uuid4().hex[:6]}"
            sess["story_id"] = story_id
            story_doc = read_story(story_id) or {}
            curr_slots = story_doc.get("slots") or {}
            base_text = compact_story_from_dialog(sess["messages"])

            scenes = summarize_and_store(user_id, story_id, base_text, curr_slots)
            threading.Thread(target=ensure_hidden_reference, args=(story_id,), daemon=True).start()
            human = "\n".join([f"{i+1}. {s}" for i,s in enumerate(scenes)])
            line_bot_api.reply_message(reply_token, TextSendMessage("整理好了！\n\n"+human+"\n\n要畫哪一段？（如：畫第一段）"))
            return

        # 3) 畫第 N 段
        if re.search(r"(畫第[一二三四五12345]段)", text):
            n = chinese_index_to_int(text)
            if n == -1:
                line_bot_api.reply_message(reply_token, TextSendMessage("請告訴我要畫第幾段（1~5）"))
                return
            story_id = sess.get("story_id")
            if not story_id:
                story_id = f"{user_id}-{uuid.uuid4().hex[:6]}"
                sess["story_id"] = story_id
                save_story(story_id, {"user_id": user_id, "created_at": firestore.SERVER_TIMESTAMP, "slots": {}})

            story_doc = read_story(story_id) or {}
            if not story_doc.get("scenes_text"):
                base_text = compact_story_from_dialog(sess["messages"])
                scenes = summarize_and_store(user_id, story_id, base_text, story_doc.get("slots") or {})
                threading.Thread(target=ensure_hidden_reference, args=(story_id,), daemon=True).start()
                human = "\n".join([f"{i+1}. {s}" for i,s in enumerate(scenes)])
                line_bot_api.reply_message(reply_token, TextSendMessage("先幫你整理了！\n\n"+human+"\n\n我開始畫指定段落囉～"))
            extra = re.sub(r"畫第[一二三四五12345]段", "", text).strip(" ，,。.!！")
            line_bot_api.reply_message(reply_token, TextSendMessage(f"收到！我開始畫第 {n} 段，完成就傳給你～"))

            def bg_job():
                with GEN_SEMAPHORE:
                    try:
                        url = generate_scene_image(sess["story_id"], n, extra=extra)
                        line_bot_api.push_message(user_id, [
                            TextSendMessage(f"第 {n} 段完成！"),
                            ImageSendMessage(url, url)
                        ])
                        save_chat(user_id, "assistant", f"[image]{url}")
                    except RuntimeError as e:
                        if str(e) == "OPENAI_ORG_NOT_VERIFIED":
                            line_bot_api.push_message(user_id, TextSendMessage(
                                "圖像生成功能尚未啟用：你的 OpenAI 組織未通過 Verify。\n"
                                "請到 OpenAI Platform → Organization → General → Verify Organization。\n"
                                "完成後數分鐘再試一次。"
                            ))
                        else:
                            print("❌ RuntimeError:", repr(e))
                            traceback.print_exc()
                            line_bot_api.push_message(user_id, TextSendMessage("這段暫時畫不出來。已記錄完整錯誤在日誌，請稍後再試或換個描述。"))
                    except Exception as e:
                        print("❌ 生成第N段失敗：", repr(e))
                        traceback.print_exc()
                        line_bot_api.push_message(user_id, TextSendMessage("這段暫時畫不出來。已記錄完整錯誤在日誌，請稍後再試或換個描述。"))
            threading.Thread(target=bg_job, daemon=True).start()
            return

        # 4) 一般對話：抽 slot → 合併 → 只問缺的
        story_id = sess.get("story_id") or f"{user_id}-{uuid.uuid4().hex[:6]}"
        sess["story_id"] = story_id
        story_doc = read_story(story_id) or {}
        curr_slots = story_doc.get("slots") or {}

        rough = rule_extract_slots(text)
        fine  = llm_extract_slots(text)
        merged = merge_slots(curr_slots, merge_slots(rough, fine))

        save_story(story_id, {"slots": merged, "updated_at": firestore.SERVER_TIMESTAMP})

        reply = format_missing_questions(merged)
        line_bot_api.reply_message(reply_token, TextSendMessage(reply))
        save_chat(user_id, "assistant", reply)

    except Exception as e:
        print("❌ handle_message error:", repr(e))
        traceback.print_exc()
        line_bot_api.reply_message(reply_token, TextSendMessage("小繪這邊出了一點狀況，等等再試試 🙇"))

# ================== 啟動 ==================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
