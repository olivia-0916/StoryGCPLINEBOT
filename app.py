# app.py  — LINE繪本機器人（隱藏定妝照 + 特徵抽取 + 5段分場景 + img2img 一致性）
import os, sys, json, re, uuid, time, tempfile, threading, traceback, random, base64, requests
from datetime import datetime
from flask import Flask, request, abort

# ---------- LINE SDK ----------
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage

# ---------- OpenAI ----------
from openai import OpenAI
# ---------- Firebase / Firestore / GCS ----------
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud import storage as gcs

# ============ 基礎設定 ============
sys.stdout.reconfigure(encoding="utf-8")
app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET      = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY           = os.environ.get("OPENAI_API_KEY")
FIREBASE_CREDENTIALS     = os.environ.get("FIREBASE_CREDENTIALS")
GCS_BUCKET               = os.environ.get("GCS_BUCKET", "storybotimage")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)
client       = OpenAI(api_key=OPENAI_API_KEY)

# ============ Firebase 初始化 ============
def _firebase_creds():
    # FIREBASE_CREDENTIALS 為 JSON 字串
    return credentials.Certificate(json.loads(FIREBASE_CREDENTIALS))
if not firebase_admin._apps:
    firebase_admin.initialize_app(_firebase_creds())
db          = firestore.client()
gcs_client  = gcs.Client()
bucket      = gcs_client.bucket(GCS_BUCKET)

# ============ 全域快取（記憶 / 任務控制） ============
GEN_SEMAPHORE     = threading.Semaphore(2)
user_sessions     = {}  # {uid: {"messages":[...], "summary":"", "scenes":[...]} }

# ============ 風格與一致性模板 ============
STYLE_PROMPT = (
    "watercolor storybook illustration, warm earthy palette, soft brush textures, "
    "clean composition, child-friendly shapes, consistent character design"
)
CONSISTENCY_GUARD = (
    "Keep the same character identity across images: same face shape, hairstyle, outfit, color palette; "
    "subtle variations only (~25%)."
)

# ============ 工具 ============
def gcs_upload_bytes(data: bytes, filename: str, content_type="image/png") -> str:
    blob = bucket.blob(f"line_images/{filename}")
    blob.upload_from_string(data, content_type=content_type)
    blob.make_public()
    return f"https://storage.googleapis.com/{GCS_BUCKET}/line_images/{filename}"

def save_story(story_id: str, data: dict):
    db.collection("stories").document(story_id).set(data, merge=True)

def read_story(story_id: str) -> dict | None:
    doc = db.collection("stories").document(story_id).get()
    return doc.to_dict() if doc.exists else None

def save_scene(story_id: str, idx: int, data: dict):
    db.collection("stories").document(story_id).collection("scenes").document(str(idx)).set(data, merge=True)

def read_scene(story_id: str, idx: int) -> dict | None:
    d = db.collection("stories").document(story_id).collection("scenes").document(str(idx)).get()
    return d.to_dict() if d.exists else None

def read_prev_image_url(story_id: str, idx: int) -> str | None:
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

# ============ LLM 助手 ============
def llm_chat(messages, temperature=0.2, model="gpt-4o-mini"):
    try:
        resp = client.chat.completions.create(model=model, messages=messages, temperature=temperature)
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print("❌ LLM error:", e)
        return ""

def split_into_five_scenes(story_text: str) -> list[str]:
    msgs = [
        {"role":"system","content":"Segment the story into exactly 5 concise scene descriptions (1–2 sentences each). Return them as a simple bullet list."},
        {"role":"user","content": story_text}
    ]
    raw = llm_chat(msgs, temperature=0.2)
    scenes = [re.sub(r"^[\-•\d\.\s]+","", s).strip() for s in raw.splitlines() if s.strip()]
    if len(scenes) < 5:
        # 補到5段，避免缺段
        scenes = (scenes + ["(empty scene)"]*5)[:5]
    else:
        scenes = scenes[:5]
    return scenes

def extract_features_from_text(story_text: str) -> dict:
    # 從文本抽角色特徵，缺的以中性值補齊
    msgs = [
        {"role":"system","content":"Extract concise reusable character descriptors as JSON keys: gender, age, hair, outfit, accessory, face, vibe. Infer neutral values if missing."},
        {"role":"user","content": story_text}
    ]
    out = llm_chat(msgs, temperature=0.2)
    try:
        data = json.loads(out)
    except Exception:
        data = {}
    # 安全預設
    data.setdefault("gender","female")
    data.setdefault("age","30s-40s")
    data.setdefault("hair","shoulder-length dark brown hair")
    data.setdefault("outfit","pink long dress")
    data.setdefault("face","gentle smile, round face")
    data.setdefault("vibe","warm, capable, kind")
    return data

def refine_features_with_image(image_url: str, curr: dict) -> dict:
    # 用首圖再精修特徵（隱藏用）
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
    except Exception:
        pass
    return curr

def build_prompt(scene_desc: str, features: dict, extra: str = "") -> str:
    role_bits = [features.get(k) for k in ["gender","age","hair","outfit","accessory","face","vibe"] if features.get(k)]
    role_str  = ", ".join(role_bits)
    return f"{STYLE_PROMPT}. Main character: {role_str}. {CONSISTENCY_GUARD} {extra} Scene: {scene_desc}"

# ============ OpenAI 圖片 ============
def openai_generate(prompt: str, size="1024x1024") -> bytes:
    r = client.images.generate(model="gpt-image-1", prompt=prompt, size=size)
    return base64.b64decode(r.data[0].b64_json)

def openai_img2img(prompt: str, ref_bytes: bytes, size="1024x1024") -> bytes:
    r = client.images.edits(
        model="gpt-image-1",
        prompt=prompt,
        image=[("image", ref_bytes, "ref.png")],
        size=size,
    )
    return base64.b64decode(r.data[0].b64_json)

# ============ 隱藏定妝照（不回覆使用者） ============
def ensure_hidden_reference(story_id: str):
    story = read_story(story_id) or {}
    feats = story.get("character_features")
    href = story.get("hidden_reference_image_url")

    if feats and href:
        return  # 都有了

    if not feats:
        txt = story.get("story_text", "")
        feats = extract_features_from_text(txt)
        save_story(story_id, {"character_features": feats})

    # 生成一張頭肩像（僅內部使用）
    headshot_prompt = build_prompt(
        "Head-and-shoulders portrait, neutral background, neutral expression, facing camera.",
        feats,
        extra="No text, no watermark."
    )
    img = openai_generate(headshot_prompt)
    url = gcs_upload_bytes(img, f"{story_id}_hidden_ref.png")
    feats = refine_features_with_image(url, feats)
    save_story(story_id, {"character_features": feats, "hidden_reference_image_url": url})

# ============ 對外：生成第 n 段 ============
def generate_scene_image(story_id:str, idx:int, extra:str="") -> str:
    story = read_story(story_id) or {}
    scenes = story.get("scenes_text") or []
    if not scenes or idx < 1 or idx > 5:
        raise ValueError("Scenes not ready or index out of range.")

    feats = story.get("character_features") or extract_features_from_text(story.get("story_text",""))
    save_story(story_id, {"character_features": feats})

    # 確保有隱藏參考圖
    ensure_hidden_reference(story_id)

    scene_text = scenes[idx-1]
    prompt     = build_prompt(scene_text, feats, extra=extra)

    # 參考來源：上一張 > 隱藏參考圖 > 直接生成
    ref_url = read_prev_image_url(story_id, idx) or (read_story(story_id) or {}).get("hidden_reference_image_url")

    if ref_url:
        rb  = requests.get(ref_url, timeout=30).content
        img = openai_img2img(prompt, rb)
    else:
        img = openai_generate(prompt)

    url = gcs_upload_bytes(img, f"{story_id}_s{idx}.png")
    save_scene(story_id, idx, {"text": scene_text, "prompt": prompt, "image_url": url})
    return url

# ============ 對話輔助 ============
def compact_story_from_dialog(messages: list[dict]) -> str:
    # 從近期對話擷取故事材料
    user_lines = [m["content"] for m in messages if m.get("role")=="user"]
    text = "\n".join(user_lines[-12:])
    return text.strip()

def summarize_to_five_scenes_and_persist(user_id: str, story_id: str, story_text: str) -> list[str]:
    scenes = split_into_five_scenes(story_text)
    save_story(story_id, {
        "user_id": user_id,
        "story_text": story_text,
        "scenes_text": scenes,
        "style_preset": "watercolor_storybook_v1",
        "updated_at": firestore.SERVER_TIMESTAMP
    })
    return scenes

def chinese_index_to_int(s: str) -> int:
    m = re.search(r"[一二三四五12345]", s)
    if not m: return -1
    map_ = {'一':1,'二':2,'三':3,'四':4,'五':5,'1':1,'2':2,'3':3,'4':4,'5':5}
    return map_[m.group(0)]

# ============ Flask 路由 ============
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

# ============ 主事件處理 ============
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text    = (event.message.text or "").strip()
    reply_token = event.reply_token
    print(f"📩 {user_id}: {text}")

    sess = user_sessions.setdefault(user_id, {"messages": [], "summary":"", "scenes":[]})
    sess["messages"].append({"role":"user","content":text})
    if len(sess["messages"]) > 60: sess["messages"] = sess["messages"][-60:]
    save_chat(user_id, "user", text)

    try:
        # 1) 啟動／開始說故事
        if re.search(r"(開始說故事|說故事|講個故事|我們來講故事吧|開始創作)", text):
            story_id = f"{user_id}-{uuid.uuid4().hex[:6]}"
            save_story(story_id, {"user_id": user_id, "created_at": firestore.SERVER_TIMESTAMP})
            sess["story_id"] = story_id
            line_bot_api.reply_message(reply_token, TextSendMessage("好的！先自由描述你的故事想法～\n隨時說「整理」我就會分成5段。"))
            return

        # 2) 整理故事 → 分成5段（不顯示定妝）
        if re.search(r"(整理|總結|summary)", text):
            story_id = sess.get("story_id") or f"{user_id}-{uuid.uuid4().hex[:6]}"
            sess["story_id"] = story_id

            base_text = compact_story_from_dialog(sess["messages"])
            if not base_text:
                line_bot_api.reply_message(reply_token, TextSendMessage("再多說一點故事內容吧，我才好整理成5段～"))
                return

            scenes = summarize_to_five_scenes_and_persist(user_id, story_id, base_text)
            # 先靜默建立角色特徵與隱藏參考（不回圖）
            threading.Thread(target=ensure_hidden_reference, args=(story_id,), daemon=True).start()

            human_readable = "\n".join([f"{i+1}. {s}" for i,s in enumerate(scenes)])
            line_bot_api.reply_message(reply_token, TextSendMessage("整理好了！以下是5段場景：\n\n" + human_readable + "\n\n要畫哪一段？（例如：畫第一段）"))
            save_chat(user_id, "assistant", "已整理成5段")
            return

        # 3) 畫第 N 段
        if re.search(r"(畫第[一二三四五12345]段)", text):
            n = chinese_index_to_int(text)
            if n == -1:
                line_bot_api.reply_message(reply_token, TextSendMessage("請告訴我要畫第幾段～（1~5）"))
                return

            story_id = sess.get("story_id")
            if not story_id:
                # 若沒有 story_id，從對話湊故事→整理→建立 story_id
                story_id = f"{user_id}-{uuid.uuid4().hex[:6]}"
                sess["story_id"] = story_id
                base_text = compact_story_from_dialog(sess["messages"])
                if not base_text:
                    line_bot_api.reply_message(reply_token, TextSendMessage("先描述一下故事，再請我整理哦～"))
                    return
                summarize_to_five_scenes_and_persist(user_id, story_id, base_text)
                threading.Thread(target=ensure_hidden_reference, args=(story_id,), daemon=True).start()

            # 允許在命令後附加要求（如：頭上有花）
            extra = re.sub(r"畫第[一二三四五12345]段", "", text).strip(" ，,。.!！")
            line_bot_api.reply_message(reply_token, TextSendMessage(f"收到！我開始畫第 {n} 段，完成就傳給你～"))

            def bg_job():
                with GEN_SEMAPHORE:
                    try:
                        url = generate_scene_image(story_id, n, extra=extra)
                        line_bot_api.push_message(user_id, [
                            TextSendMessage(f"第 {n} 段完成！"),
                            ImageSendMessage(url, url)
                        ])
                        save_chat(user_id, "assistant", f"[image]{url}")
                    except Exception as e:
                        print("❌ 生成第N段失敗：", e)
                        traceback.print_exc()
                        line_bot_api.push_message(user_id, TextSendMessage("這段暫時畫不出來，再補一點動作或場景試試？"))

            threading.Thread(target=bg_job, daemon=True).start()
            return

        # 4) 一般引導（極簡）
        # 針對缺要素小提示：主角 / 地點 / 目標 / 衝突 / 結局
        tips = []
        u = text
        if not re.search(r"(主角|角色|他|她|名字|叫)", u): tips.append("主角是誰？外觀如何？")
        if not re.search(r"(在哪|哪裡|場景|學校|城市|家裡|森林|海邊|太空)", u): tips.append("故事在哪裡發生？")
        if not re.search(r"(想要|目標|希望|打算)", u): tips.append("主角的目標是什麼？")
        if not re.search(r"(遇到|挑戰|問題|阻礙)", u): tips.append("他/她遇到什麼挑戰？")
        if not re.search(r"(最後|結果|結局|收尾)", u): tips.append("最後會怎麼結束？")
        prompt_text = "我懂了！想再補充一點嗎？\n" + (" / ".join(tips[:2]) if tips else "說「整理」我就幫你切成5段～")
        line_bot_api.reply_message(reply_token, TextSendMessage(prompt_text))
        save_chat(user_id, "assistant", prompt_text)

    except Exception as e:
        print("❌ handle_message error:", e)
        traceback.print_exc()
        line_bot_api.reply_message(reply_token, TextSendMessage("小繪這邊出了一點狀況，等等再試試 🙇"))

# ============ 啟動 ============
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
