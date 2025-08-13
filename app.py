# app.py
import os, sys, json, time, uuid, re, random, traceback, tempfile, gc, threading
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
import requests

# ---------- 基礎設定 ----------
sys.stdout.reconfigure(encoding="utf-8")
app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET      = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY           = os.environ.get("OPENAI_API_KEY")
LEONARDO_API_KEY         = os.environ.get("LEONARDO_API_KEY")
FIREBASE_CREDENTIALS     = os.environ.get("FIREBASE_CREDENTIALS")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)

# ---------- Firebase / GCS ----------
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud import storage as gcs_storage

def _firebase_creds():
    return credentials.Certificate(json.loads(FIREBASE_CREDENTIALS))
firebase_admin.initialize_app(_firebase_creds())
db = firestore.client()

GCS_BUCKET = "storybotimage"
gcs_client = gcs_storage.Client()
gcs_bucket = gcs_client.bucket(GCS_BUCKET)

# ---------- Leonardo ----------
LEO_BASE  = "https://cloud.leonardo.ai/api/rest/v1"
LEO_MODEL = "7b592283-e8a7-4c5a-9ba6-d18c31f258b9"   # Lucid Origin
IMG_W = 512
IMG_H = 512

# ---------- 會話 / 記憶 ----------
user_sessions     = {}  # {uid: {"messages":[...], "summary":"", "paras":[...]} }
user_last_images  = {}  # {uid: {"url":..., "image_id":...}}
user_seeds        = {}  # {uid: int}
user_anchor_cards = {}  # {uid: {ANCHOR_ID, visual, personality, behavior, catchphrase, signature_item}}

# ---------- OpenAI ----------
def _chat(messages, temperature=0.7):
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=temperature
        )
        return resp.choices[0].message["content"].strip()
    except Exception as e:
        print("❌ OpenAI error:", e)
        return None

# ---------- 儲存工具 ----------
def save_chat(user_id, role, text):
    try:
        db.collection("users").document(user_id).collection("chat").add({
            "role": role, "text": text, "timestamp": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        print("⚠️ Firebase save_chat failed:", e)

def save_story_summary(user_id, paragraphs):
    try:
        db.collection("users").document(user_id).collection("story")\
          .document("latest_summary").set({
            "paragraphs": paragraphs,
            "updated_at": firestore.SERVER_TIMESTAMP
          })
        print("✅ 已儲存最新故事總結")
    except Exception as e:
        print("⚠️ save_story_summary 失敗：", e)

def load_latest_story_paragraphs(user_id):
    try:
        doc = db.collection("users").document(user_id).collection("story")\
               .document("latest_summary").get()
        if doc.exists:
            data = doc.to_dict()
            paras = data.get("paragraphs") or []
            if isinstance(paras, list) and paras:
                return paras[:5]
    except Exception as e:
        print("⚠️ load_latest_story_paragraphs 失敗：", e)
    return None

def save_anchor(user_id, anchor):
    try:
        db.collection("users").document(user_id).collection("story")\
          .document("anchor").set(anchor, merge=True)
        print("✅ Anchor Card 已儲存")
    except Exception as e:
        print("⚠️ save_anchor 失敗：", e)

def load_anchor(user_id):
    try:
        doc = db.collection("users").document(user_id).collection("story")\
               .document("anchor").get()
        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        print("⚠️ load_anchor 失敗：", e)
    return None

# ---------- GCS 上傳 ----------
def upload_to_gcs_from_url(url, user_id, prompt):
    tmp_path = None
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            fd, tmp_path = tempfile.mkstemp(prefix="img_", suffix=".png", dir="/tmp")
            with os.fdopen(fd, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024*64):
                    if chunk:
                        f.write(chunk)
        filename = f"{user_id}_{uuid.uuid4().hex}.png"
        blob = gcs_bucket.blob(filename)
        blob.upload_from_filename(tmp_path, content_type="image/png")
        gcs_url = f"https://storage.googleapis.com/{GCS_BUCKET}/{filename}"
        db.collection("users").document(user_id).collection("images").add({
            "url": gcs_url, "prompt": (prompt or "")[:1500], "timestamp": firestore.SERVER_TIMESTAMP
        })
        print("✅ 圖片已上傳至 GCS 並儲存：", gcs_url)
        return gcs_url
    except Exception as e:
        print("❌ GCS upload failed:", e)
        return None
    finally:
        try:
            if tmp_path and os.path.exists(tmp_path): os.remove(tmp_path)
        except Exception:
            pass
        gc.collect()

# ---------- 摘要 ----------
def generate_story_summary(messages):
    prompt = (
        "請將以下對話整理成 5 段完整故事，每段 2–3 句（約 60–120 字），"
        "每段需自然呈現場景、角色、主要動作與關鍵物件，但不要列提綱或加註。"
        "用條列 1.~5.，只輸出故事內容，不要標題。"
    )
    msgs = [{"role":"system","content":prompt}] + messages
    return _chat(msgs, temperature=0.5)

def extract_paragraphs(summary):
    if not summary: return []
    lines = [re.sub(r"^\d+\.\s*","",x.strip()) for x in summary.split("\n") if x.strip()]
    return lines[:5]

# ---------- 規範化（中→英片語） ----------
ZH2EN_COLOR = {
    "灰色":"gray","黑色":"black","白色":"white","紅色":"red","藍色":"blue","綠色":"green",
    "黃色":"yellow","粉色":"pink","紫色":"purple","橙色":"orange","棕色":"brown","咖啡色":"brown"
}
ZH2EN_CLOTHING = {
    "長裙":"long skirt","短裙":"short skirt","連衣裙":"dress","碎花":"floral","花色":"floral",
    "襯衫":"shirt","T恤":"t-shirt","外套":"jacket","大衣":"coat","背帶褲":"suspenders"
}
def normalize_colors(text):
    if not text: return text
    for zh,en in ZH2EN_COLOR.items(): text = re.sub(zh, en, text)
    return text
def normalize_visual_freeform(text):
    text = normalize_colors(text or "")
    # 常見中文服飾詞翻成英文關鍵詞
    for zh,en in ZH2EN_CLOTHING.items():
        text = re.sub(zh, en, text)
    return text

# ---------- Anchor（錨定） ----------
def ensure_anchor(user_id):
    if user_id in user_anchor_cards and user_anchor_cards[user_id]:
        return user_anchor_cards[user_id]
    loaded = load_anchor(user_id)
    if loaded:
        user_anchor_cards[user_id] = loaded
        return loaded
    # 初始空卡（等保底推斷）
    card = {
        "ANCHOR_ID": uuid.uuid4().hex[:6],
        "visual": "", "personality": "", "behavior": "",
        "catchphrase": "", "signature_item": ""
    }
    user_anchor_cards[user_id] = card
    save_anchor(user_id, card)
    return card

def merge_anchor(user_id, patch):
    card = ensure_anchor(user_id)
    for k,v in (patch or {}).items():
        if v: card[k] = v
    if not card.get("ANCHOR_ID"):
        card["ANCHOR_ID"] = uuid.uuid4().hex[:6]
    user_anchor_cards[user_id] = card
    save_anchor(user_id, card)
    return card

def anchor_text(card):
    aid = card.get("ANCHOR_ID","????")
    visual = card.get("visual","human, simple outfit")
    personality = card.get("personality","kind, reliable")
    behavior = card.get("behavior","helps family efficiently")
    sig = card.get("signature_item","")
    base = [
        f"ANCHOR::{aid}",
        f"CHARACTER BIBLE (DO NOT CHANGE): Main character visual: {visual}.",
        "Keep face, hairstyle (length/shape), outfit items, color palette, and body proportions CONSISTENT in all images.",
        "Do NOT change age/gender/ethnicity/hairstyle/outfit/colors unless explicitly instructed."
    ]
    if sig:
        base.append(f"Signature item: {sig}. Show when appropriate.")
    base += [
        f"PERSONALITY: {personality}.",
        f"BEHAVIOR: {behavior}.",
        f"ANCHOR::{aid}"
    ]
    return "\n".join(base)

# —— 保底：從故事內容自動推斷角色藍圖（無需使用者多說）
def infer_anchor_from_story(user_id, paragraphs, raw_context):
    """
    用當前 5 段（或最後對話）推斷主角的視覺/性格/行為/口頭禪/標誌物，全部輸出英文短語。
    """
    sysmsg = (
        "You are a precise story analyst. Based on the given Chinese story paragraphs or notes, "
        "infer the MAIN CHARACTER's blueprint and output JSON with keys: "
        "visual (EN short phrases: hair/eyes/clothes/colors/marks), personality (comma-separated EN traits), "
        "behavior (usual actions/reactions), catchphrase (if any), signature_item (if any). "
        "Be concise; avoid extra text; use lowercase English nouns/adjectives."
    )
    content = "Paragraphs:\n" + "\n".join(paragraphs or []) + "\n\nRaw context:\n" + (raw_context or "")
    res = _chat([{"role":"system","content":sysmsg},{"role":"user","content":content}], temperature=0.2)
    try:
        data = json.loads(res)
    except Exception:
        # 後備一個「職業媽媽」類預設，符合你示例
        data = {
            "visual": "short hair, floral long skirt, office casual, warm smile",
            "personality": "caring, efficient, calm under pressure",
            "behavior": "teleports to help family, balances work and home",
            "catchphrase": "",
            "signature_item": "work laptop"
        }
    # 視覺短語做基本規範化
    data["visual"] = normalize_visual_freeform(data.get("visual",""))
    return data

# ---------- 場景分析（簡版） ----------
def analyze_scene(paragraph):
    scene = {
        "setting": "general location", "time_of_day": "day", "mood": "neutral",
        "foreground": "main character in action", "background": "environment",
        "main_action": "performing an action", "interaction": "natural interaction", "key_objects": ""
    }
    if re.search(r"(森林|樹林|公園|花園)", paragraph): scene["setting"] = "forest/nature"
    elif re.search(r"(城市|街道|大樓|商店|台北|臺北)", paragraph): scene["setting"] = "urban/city"
    elif re.search(r"(家裡|房間|廚房|客廳)", paragraph): scene["setting"] = "home/indoor"
    elif re.search(r"(學校|教室|操場|圖書館)", paragraph): scene["setting"] = "school/educational"

    if re.search(r"(夜晚|晚上|深夜|月光)", paragraph): scene["time_of_day"] = "night"
    elif re.search(r"(早晨|早上|日出|黃昏)", paragraph): scene["time_of_day"] = "morning/sunset"

    if re.search(r"(快樂|開心|興奮|歡樂)", paragraph): scene["mood"] = "happy/joyful"
    elif re.search(r"(悲傷|難過|憂鬱|緊張)", paragraph): scene["mood"] = "sad/melancholy"
    elif re.search(r"(神秘|奇幻|冒險|刺激)", paragraph): scene["mood"] = "mysterious/adventurous"
    return scene

# ---------- 圖像 Prompt（錨定放最前，無廢話） ----------
def build_image_prompt(user_id, scene, user_extra_desc=""):
    card = ensure_anchor(user_id)
    anchor = anchor_text(card)
    style_guard = ("STYLE: storybook watercolor illustration, wholesome, family-friendly. "
                   "COMPOSITION: full scene; visible environment; avoid plain white background; avoid isolated headshots.")
    scene_desc = (
        f"SCENE: setting={scene.get('setting','general location')}, "
        f"time_of_day={scene.get('time_of_day','day')}, mood={scene.get('mood','neutral')}, "
        f"foreground action={scene.get('main_action','performing an action')}, "
        f"background={scene.get('background','environment')}, "
        f"interaction={scene.get('interaction','natural interaction')}, "
        f"key_objects={scene.get('key_objects','none')}."
    )
    parts = [anchor, style_guard, scene_desc]
    if user_extra_desc:
        parts.append("USER ADDITIONS: " + normalize_visual_freeform(user_extra_desc))
    prompt = " ".join(parts)
    negative = (
        "text, letters, words, captions, subtitles, watermark, signature, "
        "plain studio white background, poster layout, close-up headshot only, "
        "different character, different face, different hairstyle, different outfit, different colors, "
        "age change, gender change, extra characters, multiple versions of the main character"
    )
    return prompt, negative

# ---------- Leonardo API ----------
def leonardo_headers():
    return {
        "Authorization": f"Bearer {LEONARDO_API_KEY.strip()}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

def leonardo_tti(payload):
    url = f"{LEO_BASE}/generations"
    r = requests.post(url, headers=leonardo_headers(), json=payload, timeout=60)
    if not r.ok:
        print("❌ Leonardo POST 失敗:", r.status_code, r.text)
    r.raise_for_status()
    data = r.json()
    return data["sdGenerationJob"]["generationId"]

def leonardo_poll(gen_id, timeout=180):
    url = f"{LEO_BASE}/generations/{gen_id}"
    start = time.time()
    while time.time()-start < timeout:
        time.sleep(4)
        try:
            r = requests.get(url, headers=leonardo_headers(), timeout=30)
            if not r.ok:
                print(f"❌ Leonardo GET 失敗: {r.status_code}, {r.text}")
                continue
            r.raise_for_status()
            data = r.json()
            if data.get("generations_by_pk"):
                generation_data = data["generations_by_pk"]
                status = generation_data.get("status")
                if status == "COMPLETE":
                    images = generation_data.get("generated_images", [])
                    if images:
                        gi = images[0]
                        return gi.get("url"), gi.get("id")
                    return None, None
                elif status == "FAILED":
                    return None, None
            else:
                print(f"⚠️ 回應格式異常: {data}")
        except Exception as e:
            print("❌ poll error:", e)
            traceback.print_exc()
    print(f"⏰ 輪詢超時 ({timeout}s)，生成 ID: {gen_id}")
    return None, None

def generate_leonardo_image(*, user_id, prompt, negative_prompt, seed, init_image_id=None, init_strength=None):
    payload = {
        "modelId": LEO_MODEL,
        "prompt": prompt[:1500],
        "num_images": 1,
        "width": IMG_W, "height": IMG_H,
        "ultra": False,
        "enhancePrompt": False,
        "negative_prompt": negative_prompt,
        "seed": int(seed)
    }
    if init_image_id and init_strength is not None:
        payload["init_image_id"] = init_image_id
        payload["init_strength"] = float(init_strength)

    print("🎨 Leonardo payload =>", json.dumps(payload, ensure_ascii=False)[:900])
    try:
        gen_id = leonardo_tti(payload)
        url, image_id = leonardo_poll(gen_id)
        if url:
            gcs_url = upload_to_gcs_from_url(url, user_id, prompt)
            return {"url": gcs_url, "image_id": image_id}
    except requests.HTTPError as e:
        if init_image_id and "Unexpected variable" in str(e):
            print("↩️ 降級 t2i 重試")
            return generate_leonardo_image(
                user_id=user_id, prompt=prompt, negative_prompt=negative_prompt,
                seed=seed, init_image_id=None, init_strength=None
            )
        print("❌ Leonardo HTTP 錯誤：", e)
    except Exception as e:
        print("❌ Leonardo 其他錯誤：", e)
        traceback.print_exc()
    return None

# ---------- 對話引導（保持極簡） ----------
base_system_prompt = (
    "你是「小繪」，協助用戶創作故事與插圖。請用簡潔口吻回應；必要時才提問。"
)

def format_reply(text):
    return re.sub(r'([。！？])\s*', r'\1\n', text)

# ---------- Flask ----------
@app.route("/")
def root():
    return "LINE GPT Webhook is running!"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    if not signature:
        print("⚠️ Missing X-Line-Signature — non-LINE request. Ignored.")
        return "OK"
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ---------- 狀態工具 ----------
def reset_session(user_id):
    user_sessions[user_id] = {"messages": [], "summary": "", "paras": []}
    user_seeds[user_id] = random.randint(100000, 999999)
    user_last_images[user_id] = {}
    ensure_anchor(user_id)
    print(f"✅ Reset session for {user_id}, seed={user_seeds[user_id]}")

# ---------- 背景任務（無廢話） ----------
GEN_SEMAPHORE = threading.Semaphore(2)

def _ensure_paragraphs(user_id, sess):
    paras = load_latest_story_paragraphs(user_id) or sess.get("paras") or []
    if not paras:
        # 從最近對話蒐集故事性訊息
        noise = re.compile(r"(幫我畫第|請畫第|畫第|整理|總結|定妝|角色設定|更新角色)")
        story_texts = [m["content"] for m in sess["messages"] if m.get("role")=="user" and not noise.search(m.get("content",""))]
        if story_texts:
            compact = [{"role":"user","content":"\n".join(story_texts[-8:])}]
            summary = generate_story_summary(compact)
            sess["summary"] = summary
            paras = extract_paragraphs(summary)
            sess["paras"] = paras
            if paras: save_story_summary(user_id, paras)
    return paras

def _ensure_anchor_from_story(user_id, paras, sess):
    card = ensure_anchor(user_id)
    # 已經有 visual 就不推斷
    if card.get("visual"):
        return card
    raw_ctx = "\n".join([m["content"] for m in sess["messages"][-12:]])
    inferred = infer_anchor_from_story(user_id, paras, raw_ctx)
    # 合併到卡
    merge_anchor(user_id, inferred)
    print(f"🧭 保底 Anchor 已推斷：{user_anchor_cards[user_id]}")
    return user_anchor_cards[user_id]

def _maybe_do_silent_portrait(user_id):
    """若尚無定妝參考，靜默做一張基準照，不發任何訊息"""
    ref = user_last_images.get(user_id, {}).get("image_id")
    if ref: return ref
    seed = user_seeds.setdefault(user_id, random.randint(100000,999999))
    prompt = anchor_text(ensure_anchor(user_id)) + " Full body character portrait, neutral pose, clear outfit and colors. Watercolor illustration."
    result = generate_leonardo_image(
        user_id=user_id, prompt=prompt,
        negative_prompt="text, letters, words, captions, subtitles, watermark, signature, plain studio background",
        seed=seed
    )
    if result and result["url"]:
        user_last_images[user_id] = {"url": result["url"], "image_id": result["image_id"]}
        return result["image_id"]
    return None

def bg_draw_segment(user_id, n, extra_desc):
    """真正畫圖：無中間話術；成功只送圖，失敗回一行訊息"""
    with GEN_SEMAPHORE:
        try:
            sess = user_sessions.setdefault(user_id, {"messages": [], "summary": "", "paras": []})
            paras = _ensure_paragraphs(user_id, sess)
            if not paras or n >= len(paras):
                line_bot_api.push_message(user_id, TextSendMessage("資訊不足，請先補充這段內容。"))
                return

            # 保底：若沒角色卡，先從段落推斷
            _ensure_anchor_from_story(user_id, paras, sess)

            # 如果 extra_desc 有臨時特徵，併入 visual
            if extra_desc:
                patch = {"visual": (user_anchor_cards[user_id].get("visual","") + ", " + normalize_visual_freeform(extra_desc)).strip(", ")}
                merge_anchor(user_id, patch)

            # 定妝（靜默）
            ref_id = _maybe_do_silent_portrait(user_id)
            seed = user_seeds.setdefault(user_id, random.randint(100000,999999))

            # 生成 prompt
            scene = analyze_scene(paras[n])
            prompt, neg = build_image_prompt(user_id, scene, extra_desc)

            # 一律 i2i（除非 extra 指定“換裝/改髮/改色”）
            use_init = True
            init_strength = 0.26
            if re.search(r"(換裝|換衣|改髮|改色|change outfit|new look)", extra_desc or "", flags=re.I):
                use_init = False

            result = generate_leonardo_image(
                user_id=user_id, prompt=prompt, negative_prompt=neg,
                seed=seed,
                init_image_id=(ref_id if use_init else None),
                init_strength=(init_strength if use_init else None)
            )

            if result and result["url"]:
                user_last_images[user_id] = {
                    "url": result["url"],
                    "image_id": result.get("image_id", ref_id)
                }
                # 只送圖，不囉嗦
                line_bot_api.push_message(user_id, ImageSendMessage(result["url"], result["url"]))
                save_chat(user_id, "assistant", f"[image]{result['url']}")
            else:
                line_bot_api.push_message(user_id, TextSendMessage("這段暫時畫不出來，再補一點關鍵動作或場景試試。"))
        except Exception as e:
            print("❌ bg_draw_segment 失敗：", e)
            traceback.print_exc()
            try:
                line_bot_api.push_message(user_id, TextSendMessage("生成遇到小狀況，稍後重試。"))
            except Exception:
                pass

# ---------- 主處理 ----------
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    reply_token = event.reply_token
    print(f"📩 {user_id}：{text}")

    try:
        # 啟動
        if re.search(r"(開始說故事|說故事|講個故事|一起來講故事吧|我們來講故事吧)", text):
            reset_session(user_id)
            # 啟動後不多話
            line_bot_api.reply_message(reply_token, TextSendMessage("好的，直接描述故事或叫我畫第幾段即可。"))
            return

        # 對話緩存
        sess = user_sessions.setdefault(user_id, {"messages": [], "summary": "", "paras": []})
        sess["messages"].append({"role":"user","content":text})
        if len(sess["messages"]) > 60: sess["messages"] = sess["messages"][-60:]
        save_chat(user_id, "user", text)

        # 使用者主動設定或更新角色（可選；保留）
        if text.startswith("角色設定"):
            # 視覺=；性格=；行為=；口頭禪=；標誌物=
            patch = {}
            m = re.search(r"視覺[:=：]\s*([^\n；;]+)", text);        patch["visual"] = normalize_visual_freeform(m.group(1)) if m else ""
            m = re.search(r"性格[:=：]\s*([^\n；;]+)", text);        patch["personality"] = m.group(1).strip() if m else ""
            m = re.search(r"行為[:=：]\s*([^\n；;]+)", text);        patch["behavior"] = m.group(1).strip() if m else ""
            m = re.search(r"口頭禪[:=：]\s*([^\n；;]+)", text);      patch["catchphrase"] = m.group(1).strip() if m else ""
            m = re.search(r"標誌物[:=：]\s*([^\n；;]+)", text);      patch["signature_item"] = normalize_colors(m.group(1).strip()) if m else ""
            merge_anchor(user_id, patch)
            line_bot_api.reply_message(reply_token, TextSendMessage("已更新角色設定。"))
            return

        if text.startswith("更新角色"):
            # 與上同，但允許只給其中幾項
            patch = {}
            for k, key in [("視覺","visual"),("性格","personality"),("行為","behavior"),("口頭禪","catchphrase"),("標誌物","signature_item")]:
                m = re.search(k + r"[:=：]\s*([^\n；;]+)", text)
                if m:
                    patch[key] = normalize_visual_freeform(m.group(1)) if key=="visual" else m.group(1).strip()
            if patch:
                merge_anchor(user_id, patch)
                line_bot_api.reply_message(reply_token, TextSendMessage("已更新角色。"))
            else:
                line_bot_api.reply_message(reply_token, TextSendMessage("請用：更新角色：視覺=…；性格=…（任選）"))
            return

        # 整理 / 總結
        if re.search(r"(整理|總結|summary)", text):
            compact = [{"role":"system","content":base_system_prompt}] + sess["messages"][-40:]
            summary = generate_story_summary(compact)
            sess["summary"] = summary
            paras = extract_paragraphs(summary)
            sess["paras"] = paras
            if paras: save_story_summary(user_id, paras)
            clean = "\n".join([f"{i+1}. {p}" for i,p in enumerate(paras)]) if paras else "資訊還不夠，請再提供情節。"
            line_bot_api.reply_message(reply_token, TextSendMessage(clean))
            return

        # 定妝（可選，會立即做，但也會在畫圖時自動保底）
        if "定妝" in text:
            line_bot_api.reply_message(reply_token, TextSendMessage("收到。"))
            threading.Thread(target=_maybe_do_silent_portrait, args=(user_id,), daemon=True).start()
            return

        # 畫第 N 段（核心：一句話下單就畫；無廢話）
        draw_pat = r"(幫我畫第[一二三四五12345]段故事的圖|請畫第[一二三四五12345]段故事的插圖|畫第[一二三四五12345]段故事的圖)"
        if re.search(draw_pat, text):
            m = re.search(r"[一二三四五12345]", text)
            idx_map = {'一':1,'二':2,'三':3,'四':4,'五':5,'1':1,'2':2,'3':3,'4':4,'5':5}
            n = idx_map.get(m.group(0),1) - 1
            # 把用戶附帶的自由文字當成臨時特徵或場景補充
            extra = re.sub(draw_pat, "", text).strip(" ，,。.!！")
            # 立即回覆極簡 ACK（避免 LINE 超時），不長篇
            line_bot_api.reply_message(reply_token, TextSendMessage(f"已開始。"))
            threading.Thread(target=bg_draw_segment, args=(user_id,n,extra), daemon=True).start()
            return

        # 其他一般訊息：不碎念
        line_bot_api.reply_message(reply_token, TextSendMessage("OK。要我畫第幾段？或輸入「整理」。"))

    except Exception as e:
        print("❌ 發生錯誤：", e)
        traceback.print_exc()
        line_bot_api.reply_message(reply_token, TextSendMessage("小繪出了一點小狀況，稍後再試 🙇"))

# ---------- 啟動 ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
