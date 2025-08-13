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

# ---------- 會話狀態 ----------
user_sessions         = {}  # {user_id: {"messages":[...], "story_mode":True, "summary":"", "paras":[...]} }
user_fixed_seed       = {}
user_character_sheet  = {}
user_definitive_imgid = {}
user_definitive_url   = {}
user_world_state      = {}
user_scene_briefs     = {}

DEFAULT_WORLD = {
    "setting": "forest",
    "time_of_day": "day",
    "mood": "calm",
    "palette": "soft watercolor palette, greens and warm light",
}
def get_world(uid):
    return user_world_state.setdefault(uid, DEFAULT_WORLD.copy())

# ---------- OpenAI ----------
def _chat(messages, temperature=0.6):
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
        print("✅ 已儲存最新故事總結（5 段）")
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

# ---------- GCS 上傳（串流，省記憶體） ----------
def upload_to_gcs_from_url(url, user_id, prompt):
    tmp_path = None
    try:
        print(f"📥 開始從 Leonardo 下載圖片: {url}")
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            print(f"✅ 圖片下載成功，開始串流處理...")
            
            fd, tmp_path = tempfile.mkstemp(prefix="img_", suffix=".png", dir="/tmp")
            with os.fdopen(fd, "wb") as f:
                chunk_count = 0
                for chunk in r.iter_content(chunk_size=1024*64):
                    if chunk:
                        f.write(chunk)
                        chunk_count += 1
                print(f"📦 圖片串流完成，共 {chunk_count} 個 chunk")
                
        filename = f"{user_id}_{uuid.uuid4().hex}.png"
        print(f"📝 準備上傳到 GCS，檔名: {filename}")
        
        blob = gcs_bucket.blob(filename)
        blob.upload_from_filename(tmp_path, content_type="image/png")
        gcs_url = f"https://storage.googleapis.com/{GCS_BUCKET}/{filename}"
        print(f"☁️ 圖片已上傳至 GCS: {gcs_url}")
        
        # 儲存到 Firestore
        db.collection("users").document(user_id).collection("images").add({
            "url": gcs_url, "prompt": (prompt or "")[:1500], "timestamp": firestore.SERVER_TIMESTAMP
        })
        print("💾 圖片資訊已儲存到 Firestore")
        
        return gcs_url
        
    except Exception as e:
        print(f"❌ GCS 上傳失敗: {e}")
        traceback.print_exc()
        return None
    finally:
        try:
            if tmp_path and os.path.exists(tmp_path): 
                os.remove(tmp_path)
                print(f"🧹 暫存檔案已清理: {tmp_path}")
        except Exception as e:
            print(f"⚠️ 清理暫存檔案失敗: {e}")
        gc.collect()
        print("♻️ 記憶體已清理")

# ---------- 故事摘要（只在要求時生成；五段乾淨文字） ----------
def generate_story_summary(messages):
    prompt = (
        "請將以下對話整理成 5 段完整故事，每段 2–3 句（約 60–120 字），"
        "每段需自然呈現場景、角色、主要動作與關鍵物件，但不要列提綱或加註。"
        "用條列 1.~5.，只輸出故事內容，不要標題、不加多餘說明。"
    )
    msgs = [{"role":"system","content":prompt}] + messages
    return _chat(msgs, temperature=0.5)

def extract_paragraphs(summary):
    if not summary: return []
    lines = [re.sub(r"^\d+\.\s*","",x.strip()) for x in summary.split("\n") if x.strip()]
    return lines[:5]

# ---------- 場景 brief（內部用，不顯示） ----------
def build_scene_brief(paragraph, world_hint=None):
    sysmsg = (
        "你是資深繪本分鏡師。從段落提煉【場景、時間、氛圍、前景/背景重點、主角動作/情緒、與物/人的互動、關鍵物件】，"
        "輸出 JSON（keys: setting, time_of_day, mood, foreground, background, main_action, interaction, key_objects）。"
        "若段落未明確地點，承襲 world_hint.setting。若未提時間/氛圍，也承襲 world_hint。所有值用簡短英文片語。"
    )
    user = f"段落：{paragraph}\nworld_hint：{json.dumps(world_hint or {}, ensure_ascii=False)}"
    res = _chat([{"role":"system","content":sysmsg},{"role":"user","content":user}], temperature=0.2)
    try:
        data = json.loads(res)
        def _fallback(k, d):
            return data.get(k) or (world_hint or {}).get(k) or d
        return {
            "setting":     _fallback("setting","forest"),
            "time_of_day": _fallback("time_of_day","day"),
            "mood":        _fallback("mood","calm"),
            "foreground":  data.get("foreground","main character performing the action"),
            "background":  data.get("background","environmental elements supporting story"),
            "main_action": data.get("main_action","walking"),
            "interaction": data.get("interaction","natural interaction with objects or people"),
            "key_objects": data.get("key_objects","")
        }
    except Exception:
        return {
            "setting": (world_hint or {}).get("setting","forest"),
            "time_of_day": (world_hint or {}).get("time_of_day","day"),
            "mood": (world_hint or {}).get("mood","calm"),
            "foreground": "main character in action",
            "background": "environment details",
            "main_action": "walking",
            "interaction": "looking / pointing / holding",
            "key_objects": ""
        }

# ---------- 圖像 Prompt ----------
def build_image_prompt(user_id, scene_brief, user_extra_desc=""):
    character = user_character_sheet.get(user_id) or (
        "Consistent main character across all images. Same face, hairstyle, clothing, colors, proportions. "
        "Whimsical watercolor storybook style. Primary ethnicity: East Asian features; black hair, dark brown eyes, warm fair skin. "
        "If user does not specify otherwise, keep East Asian facial structure and same hairstyle. "
        "Signature outfit/items must appear on the main character only."
    )
    world = get_world(user_id)

    hard_rules = (
        "Compose a full scene (not a centered portrait). "
        "Show environment and story action. "
        "Exactly one main character unless the story explicitly mentions others. "
        "No plain white or blank backgrounds."
    )

    parts = [
        character,
        "family-friendly, wholesome, uplifting tone, modest clothing, safe for work, non-violent.",
        hard_rules,
        f"Scene description: setting: {scene_brief.get('setting', world['setting'])}, ",
        f"time of day: {scene_brief.get('time_of_day', world['time_of_day'])}, ",
        f"mood: {scene_brief.get('mood', world['mood'])}, ",
        f"foreground: {scene_brief.get('foreground','')}, ",
        f"background: {scene_brief.get('background','')}, ",
        f"main character action: {scene_brief.get('main_action','')}, ",
        f"interaction: {scene_brief.get('interaction','')}, ",
        f"key objects: {scene_brief.get('key_objects','')}.",
    ]
    if user_extra_desc:
        parts.append(f"User additions: {user_extra_desc}")
    prompt = " ".join(parts)

    neg = (
        "text, letters, words, captions, subtitles, watermark, signature, "
        "multiple main characters, collage, grid, duplicated subject, "
        "plain white background, empty background, studio backdrop, "
        "different character, change hairstyle, change outfit, age change, gender change, "
        "blonde hair, red hair, light brown hair, blue eyes, green eyes, non-East-Asian facial features"
    )
    return prompt, neg


# ---------- Leonardo 調用 ----------
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

def leonardo_poll(gen_id, timeout=150):
    url = f"{LEO_BASE}/generations/{gen_id}"
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(4)
        try:
            r = requests.get(url, headers=leonardo_headers(), timeout=30)
            if not r.ok:
                print("❌ Leonardo GET 失敗:", r.status_code, r.text)
                continue
            data = r.json()

            # ✅ 新格式
            if data.get("generations_v2"):
                g = data["generations_v2"][0]
                status = g.get("status")
                if status == "COMPLETE":
                    gi = g["generated_images"][0]
                    return gi.get("url"), gi.get("id")
                if status == "FAILED":
                    return None, None

            # ✅ 舊格式
            if data.get("generations_by_pk"):
                g = data["generations_by_pk"]
                status = g.get("status")
                if status == "COMPLETE":
                    imgs = g.get("generated_images", [])
                    if imgs:
                        return imgs[0].get("url"), imgs[0].get("id")
                    return None, None
                if status == "FAILED":
                    return None, None

            print("⏳ 等待中…", json.dumps(data, ensure_ascii=False)[:200])

        except Exception as e:
            print("❌ 輪詢異常：", e)
            traceback.print_exc()

    print(f"⏰ 輪詢超時 {timeout}s, gen_id={gen_id}")
    return None, None


def generate_leonardo_image(*, user_id, prompt, negative_prompt, seed, init_image_id=None, init_strength=None):
    print(f"🎨 開始 Leonardo 圖片生成...")
    payload = {
        "modelId": LEO_MODEL,
        "prompt": prompt[:1500],
        "num_images": 1,
        "width": IMG_W, "height": IMG_H,
        "contrast": 3.0,
        "ultra": False,
        "enhancePrompt": False,
        "negative_prompt": negative_prompt,
        "seed": int(seed)
    }

    # ✅ 正確的 img2img 參數（Leonardo）
    if init_image_id and init_strength:
        payload["isInitImage"] = True
        payload["init_generation_image_id"] = init_image_id
        payload["init_strength"] = float(init_strength)

    print("🎨 Leonardo payload =>", json.dumps(payload, ensure_ascii=False))
    try:
        gen_id = leonardo_tti(payload)
        print("✅ Leonardo Generation ID:", gen_id)
        url, image_id = leonardo_poll(gen_id)
        if url:
            gcs_url = upload_to_gcs_from_url(url, user_id, prompt)
            return {"url": gcs_url, "image_id": image_id} if gcs_url else None
        else:
            print("❌ Leonardo 圖片生成失敗或超時")
            return None
    except requests.HTTPError as e:
        # 某些舊版 schema 會對未知欄位報 400；降級成 T2I 再試一次
        if init_image_id and ("Unexpected variable" in str(e) or "bad-request" in str(e)):
            print("↩️ 自動降級：移除 init 參數改用 text-to-image 重試")
            return generate_leonardo_image(
                user_id=user_id, prompt=prompt, negative_prompt=negative_prompt,
                seed=seed, init_image_id=None, init_strength=None
            )
        print("❌ Leonardo HTTP 錯誤：", e)
        return None
    except Exception as e:
        print(f"❌ Leonardo 其他錯誤：{e}")
        traceback.print_exc()
        return None


# ---------- 引導與格式 ----------
base_system_prompt = (
    "你是「小繪」，一位親切、溫柔、擅長說故事的 AI 夥伴，協助長輩創作 5 段故事繪本。\n"
    "請用簡潔、好讀的語氣回應；每則訊息盡量不超過 35 字並適當分段。\n"
    "第一階段：以『回述 + 肯定 + 輕量補問 1–2 題』來引導補齊人事時地物與動作/情緒；不要自行總結整個故事。\n"
    "只有在使用者說「整理/總結」或要求繪圖且無段落摘要時，才產生摘要（五段乾淨段落）。\n"
    "請自稱「小繪」。"
)
def format_reply(text):
    return re.sub(r'([。！？])\s*', r'\1\n', text)

def natural_guidance(last_user_text):
    brief = last_user_text if len(last_user_text) <= 40 else last_user_text[:40] + "…"
    asks = []
    if not re.search(r"(叫|名|主角|花媽|卡卡|[A-Za-z]+)", last_user_text):
        asks.append("主角叫什麼、外觀或穿著呢？")
    if not re.search(r"(台北|森林|學校|公司|家|村|公園)", last_user_text):
        asks.append("這段在哪裡、什麼時段？")
    if not re.search(r"(遇到|準備|解決|幫助|發現|瞬間移動|旅行|尋找)", last_user_text):
        asks.append("這段想發生什麼動作或轉折？")
    if not asks: asks = ["想再加哪個小細節？"]
    return f"我聽到了：{brief}\n很有畫面感！\n{asks[0]}"

# ---------- Flask 路由 ----------
@app.route("/")
def root():
    return "LINE GPT Webhook is running!"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    if not signature:
        print("⚠️ Missing X-Line-Signature — non-LINE request (axios/Postman/healthcheck?). Ignored.")
        return "OK"
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ---------- 狀態工具 ----------
def reset_session(user_id):
    user_sessions[user_id] = {"messages": [], "story_mode": True, "summary": "", "paras": []}
    user_fixed_seed[user_id] = random.randint(100000, 999999)
    user_world_state[user_id] = DEFAULT_WORLD.copy()
    user_scene_briefs[user_id] = []
    print(f"✅ Reset session for {user_id}, seed={user_fixed_seed[user_id]}")

# ---------- 背景任務：並發限制 ----------
GEN_SEMAPHORE = threading.Semaphore(2)   # 同時最多 2 個生成任務

def bg_generate_and_push_draw(user_id, n, extra_desc):
    """背景生成第 n 段插圖，完成後 push 回去"""
    print(f"🎬 開始背景生成第 {n+1} 段插圖...")
    
    with GEN_SEMAPHORE:
        try:
            sess = user_sessions.setdefault(user_id, {"messages": [], "story_mode": True, "summary": "", "paras": []})
            print(f"📚 載入用戶 {user_id} 的會話資料")
            
            paras = load_latest_story_paragraphs(user_id) or sess.get("paras") or []
            if not paras:
                print("📝 沒有找到故事段落，開始臨時整理...")
                msgs = [{"role":"system","content":base_system_prompt}] + sess["messages"][-40:]
                summary = generate_story_summary(msgs)
                sess["summary"] = summary
                paras = extract_paragraphs(summary)
                sess["paras"] = paras
                if paras: 
                    save_story_summary(user_id, paras)
                    print(f"✅ 已儲存 {len(paras)} 段故事摘要")
                else:
                    print("❌ 故事摘要生成失敗")
                    
            if not paras or n >= len(paras):
                print(f"❌ 故事段落不足，需要 {n+1} 段，但只有 {len(paras)} 段")
                line_bot_api.push_message(user_id, TextSendMessage("資訊不足，這段再給我一些細節好嗎？"))
                return

            print(f"📖 第 {n+1} 段故事內容: {paras[n][:100]}...")

            # 建 brief（如無）
            if not user_scene_briefs.get(user_id):
                print("🎭 開始建立場景簡介...")
                world = get_world(user_id)
                user_scene_briefs[user_id] = [build_scene_brief(p, world) for p in paras]
                print(f"✅ 已建立 {len(user_scene_briefs[user_id])} 個場景簡介")
                
            scene = user_scene_briefs[user_id][n]
            print(f"🎬 場景簡介: {json.dumps(scene, ensure_ascii=False)}")

            # prompt
            prompt, neg = build_image_prompt(user_id, scene, extra_desc)
            print(f"🎨 圖片 prompt: {prompt[:200]}...")
            print(f"🚫 負面 prompt: {neg[:200]}...")
            
            ref_id = user_definitive_imgid.get(user_id)
            seed   = user_fixed_seed.setdefault(user_id, random.randint(100000,999999))
            print(f"🖼️ 參考圖片 ID: {ref_id}")
            print(f"🌱 種子值: {seed}")

            result = generate_leonardo_image(
                user_id=user_id, prompt=prompt, negative_prompt=neg,
                seed=seed, init_image_id=ref_id, init_strength=0.24 if ref_id else None
            )
            
            if result and result["url"]:
                print(f"🎊 圖片生成成功！開始更新定妝參考...")
                # 更新定妝參考
                user_definitive_imgid[user_id] = result.get("image_id", ref_id) or ref_id
                user_definitive_url[user_id]   = result["url"]
                print(f"✅ 定妝參考已更新: {user_definitive_imgid[user_id]}")
                
                # 推送到 LINE
                print(f"📱 開始推送到 LINE...")
                line_bot_api.push_message(user_id, [
                    TextSendMessage(f"第 {n+1} 段完成了！"),
                    ImageSendMessage(result["url"], result["url"])
                ])
                save_chat(user_id, "assistant", f"[image]{result['url']}")
                print(f"🎉 第 {n+1} 段插圖已成功推送到用戶！")
            else:
                print("❌ 圖片生成失敗")
                line_bot_api.push_message(user_id, TextSendMessage("這段暫時畫不出來，再補充一點動作或場景試試？"))
                
        except Exception as e:
            print(f"❌ 背景生成第 {n+1} 段插圖失敗：{e}")
            traceback.print_exc()
            try:
                line_bot_api.push_message(user_id, TextSendMessage(f"生成第 {n+1} 段時遇到小狀況，等下再試一次可以嗎？"))
                print(f"📱 已向用戶發送錯誤訊息")
            except Exception as push_error:
                print(f"❌ 無法向用戶發送錯誤訊息：{push_error}")

def bg_generate_and_push_portrait(user_id):
    """背景生成定妝照"""
    print(f"🎭 開始背景生成定妝照，用戶: {user_id}")
    
    with GEN_SEMAPHORE:
        try:
            if user_character_sheet.get(user_id) is None:
                user_character_sheet[user_id] = (
                    "Consistent main character across all images. Same face, hairstyle, clothing, colors, proportions. "
                    "Whimsical watercolor storybook style. Primary ethnicity: East Asian features; black hair, dark brown eyes, warm fair skin. "
                    "Signature outfit/items must appear on the main character only."
                )
                print(f"✨ 已設定預設角色設定卡")
            else:
                print(f"📋 使用現有角色設定卡: {user_character_sheet[user_id][:100]}...")
                
            seed = user_fixed_seed.setdefault(user_id, random.randint(100000,999999))
            prompt = user_character_sheet[user_id] + " family-friendly, wholesome, uplifting tone, modest clothing, safe for work, non-violent."
            print(f"🎨 定妝照 prompt: {prompt[:200]}...")
            print(f"🌱 種子值: {seed}")
            
            result = generate_leonardo_image(
                user_id=user_id, prompt=prompt,
                negative_prompt="text, letters, words, captions, subtitles, watermark, signature",
                seed=seed
            )
            
            if result and result["url"]:
                print(f"🎊 定妝照生成成功！開始更新定妝參考...")
                user_definitive_imgid[user_id] = result["image_id"]
                user_definitive_url[user_id]   = result["url"]
                print(f"✅ 定妝參考已更新: {user_definitive_imgid[user_id]}")
                
                # 推送到 LINE
                print(f"📱 開始推送到 LINE...")
                line_bot_api.push_message(user_id, [
                    TextSendMessage("定妝照完成囉～之後會以此為基準！"),
                    ImageSendMessage(result["url"], result["url"])
                ])
                save_chat(user_id, "assistant", f"[image]{result['url']}")
                print(f"🎉 定妝照已成功推送到用戶！")
            else:
                print("❌ 定妝照生成失敗")
                line_bot_api.push_message(user_id, TextSendMessage("定妝照暫時失敗，再試一次？"))
                
        except Exception as e:
            print(f"❌ 背景定妝失敗：{e}")
            traceback.print_exc()
            try:
                line_bot_api.push_message(user_id, TextSendMessage("定妝照遇到小狀況，等下再試一次可以嗎？"))
                print(f"📱 已向用戶發送錯誤訊息")
            except Exception as push_error:
                print(f"❌ 無法向用戶發送錯誤訊息：{push_error}")

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
            line_bot_api.reply_message(reply_token, TextSendMessage("太好了！先說主角與地點吧？"))
            return

        sess = user_sessions.setdefault(user_id, {"messages": [], "story_mode": True, "summary": "", "paras": []})
        sess["messages"].append({"role":"user","content":text})
        if len(sess["messages"]) > 60: sess["messages"] = sess["messages"][-60:]
        save_chat(user_id, "user", text)

        # 使用者指定穿搭 → 更新設定卡
        if re.search(r"(穿|戴|頭上|衣|裙|襯衫|鞋|配件)", text):
            m = re.search(r"(穿|戴)(.+)", text)
            wear_txt = m.group(2).strip() if m else text
            user_character_sheet[user_id] = (
                "Consistent main character across all images. Same face, hairstyle, clothing, colors, proportions. "
                "Whimsical watercolor storybook style. Primary ethnicity: East Asian features; black hair, dark brown eyes, warm fair skin. "
                f"Main character always wears/has: {wear_txt}. Only the main character has these signature items."
            )
            print("✨ 角色設定卡已更新:", user_character_sheet[user_id])

        # 整理 / 總結（只在要求時）
        if re.search(r"(整理|總結|summary)", text):
            msgs = [{"role":"system","content":base_system_prompt}] + sess["messages"][-40:]
            summary = generate_story_summary(msgs)
            sess["summary"] = summary
            paras = extract_paragraphs(summary)
            sess["paras"] = paras
            if paras:
                save_story_summary(user_id, paras)
                clean = "\n".join([f"{i+1}. {p}" for i,p in enumerate(paras)])
                line_bot_api.reply_message(reply_token, TextSendMessage(clean))
                save_chat(user_id, "assistant", clean)
            else:
                line_bot_api.reply_message(reply_token, TextSendMessage("資訊還不夠，我們再補一些細節吧～"))
            return

        # 定妝（手動）
        if "定妝" in text:
            line_bot_api.reply_message(reply_token, TextSendMessage("收到，我先做定妝照，畫好就傳給你～"))
            threading.Thread(target=bg_generate_and_push_portrait, args=(user_id,), daemon=True).start()
            return

        # 畫第 N 段（背景生成 → push）
        draw_pat = r"(幫我畫第[一二三四五12345]段故事的圖|請畫第[一二三四五12345]段故事的插圖|畫第[一二三四五12345]段故事的圖)"
        if re.search(draw_pat, text):
            m = re.search(r"[一二三四五12345]", text)
            idx_map = {'一':1,'二':2,'三':3,'四':4,'五':5,'1':1,'2':2,'3':3,'4':4,'5':5}
            n = idx_map.get(m.group(0),1) - 1
            extra = re.sub(draw_pat, "", text).strip(" ，,。.!！")
            line_bot_api.reply_message(reply_token, TextSendMessage(f"收到，我開始畫第 {n+1} 段，完成就傳給你～"))
            threading.Thread(target=bg_generate_and_push_draw, args=(user_id,n,extra), daemon=True).start()
            return

        # 一般引導
        sysmsg = base_system_prompt
        msgs = [{"role":"system","content":sysmsg}] + sess["messages"][-12:]
        reply = _chat(msgs, temperature=0.7) or natural_guidance(text)
        reply = format_reply(reply)
        line_bot_api.reply_message(reply_token, TextSendMessage(reply))
        save_chat(user_id, "assistant", reply)

    except Exception as e:
        print("❌ 發生錯誤：", e)
        traceback.print_exc()
        line_bot_api.reply_message(reply_token, TextSendMessage("小繪出了一點小狀況，稍後再試 🙇"))

# ---------- 啟動 ----------
if __name__ == "__main__":
    # 建議在部署層設定：GUNICORN_CMD_ARGS="--workers 1 --threads 8 --timeout 180"
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

    
