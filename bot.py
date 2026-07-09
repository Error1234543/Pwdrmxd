import os, asyncio, aiohttp, aiofiles, logging, re, json, time, io, httpx
from pathlib import Path
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "")
OWNER_IDS     = [8226637107, 8356297447]
ALLOWED_GROUPS = [-1002645387857, -1003126293720]  # 2 allowed groups
FORCE_CHANNEL = "nexushubxd"                        # force join channel
USERS_FILE    = Path("auth_users.json")
DOWNLOAD_DIR  = Path("downloads")
RESUME_FILE   = Path("resume_state.json")
CREDIT_TAG    = "@xdsonic"
DOWNLOAD_DIR.mkdir(exist_ok=True)

user_sessions = {}
active_tasks  = {}
user_mode     = {}
mcq_buffer    = {}
msg_to_delete = {}   # uid -> [msg_id, msg_id, ...] to delete after /done

# ─── AUTH USERS (with expiry) ─────────────────────────────────────────────────
def load_auth_users() -> dict:
    """Returns {uid_str: expiry_iso_or_null}"""
    try:
        if USERS_FILE.exists():
            with open(USERS_FILE) as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {str(u): None for u in data}
                return data
    except: pass
    return {}

def save_auth_users(users: dict):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)

def is_owner(uid): return uid in OWNER_IDS

def is_authorized(uid):
    if uid in OWNER_IDS: return True
    users = load_auth_users()
    uid_str = str(uid)
    if uid_str not in users: return False
    expiry = users[uid_str]
    if expiry is None: return True
    return datetime.fromisoformat(expiry) > datetime.now()

def is_allowed_chat(uid, chat_id, chat_type):
    if is_owner(uid): return True
    if chat_type == "private": return True
    return chat_id in ALLOWED_GROUPS

# ─── FORCE JOIN CHECK ─────────────────────────────────────────────────────────
async def check_force_join(bot, uid) -> bool:
    try:
        member = await bot.get_chat_member(f"@{FORCE_CHANNEL}", uid)
        return member.status not in ("left", "kicked", "banned")
    except:
        return False

# ─── RESUME ───────────────────────────────────────────────────────────────────
def load_resume():
    try:
        if RESUME_FILE.exists():
            with open(RESUME_FILE) as f: return json.load(f)
    except: pass
    return {}

def save_resume(state):
    try:
        with open(RESUME_FILE,"w") as f: json.dump(state,f,indent=2)
    except Exception as e: logger.error(f"Resume save: {e}")

# ─── PARSE LINKS ──────────────────────────────────────────────────────────────
def parse_links_file(text):
    entries = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or "https://" not in line: continue
        idx = line.rfind(":https://")
        if idx != -1: name, url = line[:idx].strip(), "https://"+line[idx+9:].strip()
        else:
            parts = line.split(" https://", 1)
            if len(parts)==2: name, url = parts[0].strip(), "https://"+parts[1].strip()
            else: continue
        if name and url: entries.append({"name":name,"url":url})
    return entries

# ─── PARSE MCQ ────────────────────────────────────────────────────────────────
def parse_questions(text):
    questions = []
    cleaned_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r'^\[Page\s+\d+.*skip\]', stripped, re.IGNORECASE): continue
        if len(stripped) > 100 and len(set(stripped)) < 10: continue
        cleaned_lines.append(line)
    text = "\n".join(cleaned_lines)

    parts = re.split(r'(?=Q\d+[.)])', text)
    for part in parts:
        part = part.strip()
        if not part: continue
        block = re.sub(r'^Q\d+[.)]\s*', '', part, count=1)
        lines = [x.strip() for x in block.split("\n") if x.strip()]
        if len(lines) < 3: continue
        question = lines[0]
        options, answer = [], None
        for line in lines[1:]:
            if re.match(r'^\(\d+\)', line):
                opt_text = re.sub(r'^\(\d+\)\s*','',line).strip()
                options.append(opt_text)
            elif re.match(r'^\d+[.)]\s',line):
                opt_text = re.sub(r'^\d+[.)]\s*','',line).strip()
                options.append(opt_text)
            elif line.upper().startswith("ANS:"):
                try: answer = int(re.split(r':',line,1)[1].strip()) - 1
                except: pass
        valid_options = [o for o in options if o]
        if (question and len(valid_options) >= 2 and len(valid_options) == len(options)
                and answer is not None and 0 <= answer < len(options)):
            questions.append((question, valid_options, answer))
    return questions

# ─── FORMAT HELPERS ───────────────────────────────────────────────────────────
def fmt_size(b):
    if b<1024: return f"{b} B"
    elif b<1048576: return f"{b/1024:.1f} KB"
    elif b<1073741824: return f"{b/1048576:.1f} MB"
    return f"{b/1073741824:.2f} GB"
def fmt_speed(bps): return f"{fmt_size(int(bps))}/s"
def progress_bar(done,total,width=12):
    if total==0: return "▓"*width
    f=int(width*done/total); return "▓"*f+"░"*(width-f)

# ─── SAFE WRAPPERS ────────────────────────────────────────────────────────────
async def safe_edit(msg, text, retries=5):
    for i in range(retries):
        try: await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN); return
        except RetryAfter as e: await asyncio.sleep(e.retry_after+1)
        except TimedOut: await asyncio.sleep(3)
        except NetworkError: await asyncio.sleep(5)
        except Exception as e:
            if "Message is not modified" in str(e): return
            await asyncio.sleep(2)

async def safe_send(bot, chat_id, text, retries=5):
    for i in range(retries):
        try: return await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
        except RetryAfter as e: await asyncio.sleep(e.retry_after+1)
        except (TimedOut,NetworkError): await asyncio.sleep(5)
        except Exception as e: logger.warning(f"Send({i+1}): {e}"); await asyncio.sleep(3)
    return None

async def safe_send_doc(bot, chat_id, filepath, filename, caption, retries=5):
    for i in range(retries):
        try:
            with open(filepath,"rb") as f:
                return await bot.send_document(
                    chat_id=chat_id, document=f, filename=filename,
                    caption=caption, parse_mode=ParseMode.MARKDOWN,
                    read_timeout=120, write_timeout=120, connect_timeout=30)
        except RetryAfter as e: await asyncio.sleep(e.retry_after+2)
        except (TimedOut,NetworkError) as e: logger.warning(f"Upload({i+1}):{e}"); await asyncio.sleep(10)
        except Exception as e: logger.error(f"Upload({i+1}):{e}"); await asyncio.sleep(5)
    return None

async def delete_messages(bot, chat_id, msg_ids: list):
    for mid in msg_ids:
        try: await bot.delete_message(chat_id, mid)
        except: pass
        await asyncio.sleep(0.3)

# ─── DOWNLOAD ─────────────────────────────────────────────────────────────────
async def download_file(url, filepath, progress_cb=None, retries=4):
    for attempt in range(retries):
        try:
            timeout = aiohttp.ClientTimeout(total=600, connect=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status!=200: await asyncio.sleep(3); continue
                    total = int(resp.headers.get("Content-Length",0))
                    downloaded = 0; start = time.time()
                    async with aiofiles.open(filepath,"wb") as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            await f.write(chunk); downloaded+=len(chunk)
                            if progress_cb:
                                elapsed=time.time()-start
                                await progress_cb(downloaded,total,downloaded/elapsed if elapsed>0 else 0)
            return True
        except asyncio.CancelledError: raise
        except Exception as e:
            logger.error(f"Download({attempt+1}): {e}")
            if filepath.exists(): filepath.unlink()
            if attempt<retries-1: await asyncio.sleep(5*(attempt+1))
    return False

# ─── HTML BUILDER (from pasted MCQ) ──────────────────────────────────────────
def build_quiz_html(title: str, questions: list) -> str:
    q_data = [{"q": q, "opts": opts, "ans": ans} for (q, opts, ans) in questions]
    q_json = json.dumps(q_data, ensure_ascii=False)
    safe_title = title.replace('"','&quot;').replace('<','&lt;').replace('>','&gt;')
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{safe_title}</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0a0a0f;--surface:#111118;--surface2:#1a1a24;--border:#2a2a3a;--accent:#7c6af7;--accent2:#f7c76a;--accent3:#6af7c7;--text:#e8e8f0;--muted:#7070a0;--danger:#f76a6a;--success:#6af7a0;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;min-height:100vh;}}
body::before{{content:'';position:fixed;inset:0;background-image:linear-gradient(var(--border) 1px,transparent 1px),linear-gradient(90deg,var(--border) 1px,transparent 1px);background-size:40px 40px;opacity:0.2;pointer-events:none;z-index:0;}}
.app{{position:relative;z-index:1;max-width:780px;margin:0 auto;padding:28px 16px;}}
.header{{text-align:center;margin-bottom:28px;}}
.badge{{display:inline-block;background:linear-gradient(135deg,var(--accent),var(--accent3));color:#fff;font-size:10px;font-weight:700;letter-spacing:3px;padding:4px 14px;border-radius:20px;text-transform:uppercase;margin-bottom:12px;}}
.header h1{{font-size:clamp(22px,5vw,38px);font-weight:800;background:linear-gradient(135deg,#fff 30%,var(--accent));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;line-height:1.15;margin-bottom:6px;}}
.header p{{color:var(--muted);font-size:12px;font-family:'JetBrains Mono',monospace;}}
.panel{{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:22px;margin-bottom:16px;position:relative;overflow:hidden;}}
.panel::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--accent),var(--accent3),var(--accent2));}}
.quiz-meta{{display:flex;gap:10px;flex-wrap:wrap;align-items:center;justify-content:space-between;margin-bottom:16px;}}
.meta-badge{{background:var(--surface2);border:1px solid var(--border);border-radius:20px;padding:5px 12px;font-size:12px;font-family:'JetBrains Mono',monospace;color:var(--muted);}}
.progress-bar{{height:4px;background:var(--surface2);border-radius:2px;margin-bottom:20px;overflow:hidden;}}
.progress-fill{{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent3));border-radius:2px;transition:width .4s ease;}}
.question-card{{background:var(--surface2);border:1px solid var(--border);border-radius:14px;padding:20px;animation:fadeUp .3s ease;}}
@keyframes fadeUp{{from{{opacity:0;transform:translateY(10px)}}to{{opacity:1;transform:translateY(0)}}}}
.q-header{{display:flex;gap:12px;align-items:flex-start;margin-bottom:16px;}}
.q-num{{background:var(--accent);color:#fff;font-size:11px;font-weight:700;padding:4px 10px;border-radius:6px;white-space:nowrap;font-family:'JetBrains Mono',monospace;margin-top:2px;flex-shrink:0;}}
.q-text{{font-size:15px;font-weight:600;line-height:1.55;}}
.options-grid{{display:grid;gap:9px;}}
.opt-btn{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px;text-align:left;color:var(--text);font-family:'Syne',sans-serif;font-size:14px;cursor:pointer;transition:all .2s;display:flex;gap:10px;align-items:center;width:100%;}}
.opt-btn:hover:not(:disabled){{border-color:var(--accent);background:rgba(124,106,247,0.08);transform:translateX(4px);}}
.opt-btn.correct{{border-color:var(--success);background:rgba(106,247,160,0.08);color:var(--success);}}
.opt-btn.wrong{{border-color:var(--danger);background:rgba(247,106,106,0.08);color:var(--danger);}}
.opt-btn:disabled{{cursor:default;}}
.opt-letter{{font-size:11px;font-weight:700;font-family:'JetBrains Mono',monospace;width:22px;height:22px;border-radius:5px;background:var(--surface2);display:flex;align-items:center;justify-content:center;flex-shrink:0;}}
.explain-box{{background:rgba(106,247,199,.06);border:1px solid rgba(106,247,199,.2);border-radius:10px;padding:11px 14px;margin-top:12px;font-size:13px;color:var(--accent3);}}
.nav-row{{display:flex;gap:10px;justify-content:space-between;margin-top:14px;align-items:center;flex-wrap:wrap;}}
.btn{{background:linear-gradient(135deg,var(--accent),#5b4fd4);color:#fff;border:none;border-radius:10px;padding:10px 18px;font-family:'Syne',sans-serif;font-size:13px;font-weight:700;cursor:pointer;transition:all .2s;}}
.btn:hover{{transform:translateY(-2px);box-shadow:0 6px 20px rgba(124,106,247,0.3);}}
.btn:disabled{{opacity:.4;cursor:not-allowed;transform:none;}}
.btn-outline{{background:transparent;border:1px solid var(--border);color:var(--muted);}}
.btn-outline:hover{{border-color:var(--accent);color:var(--accent);box-shadow:none;}}
.btn-success{{background:linear-gradient(135deg,var(--success),#2a9a60);color:#000;}}
#score-card{{display:none;text-align:center;padding:30px 18px;animation:fadeUp .4s ease;}}
.score-ring{{width:130px;height:130px;margin:0 auto 18px;position:relative;}}
.score-ring svg{{transform:rotate(-90deg);}}
.ring-bg{{fill:none;stroke:var(--surface2);stroke-width:8;}}
.ring-fill{{fill:none;stroke-width:8;stroke-linecap:round;stroke-dasharray:0 314;transition:stroke-dasharray 1s ease;}}
.score-center{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;}}
.big-score{{font-size:28px;font-weight:800;line-height:1;}}
.score-label{{font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace;margin-top:3px;}}
.grade-badge{{font-size:24px;font-weight:800;margin-bottom:5px;}}
.score-stats{{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin:14px 0;}}
.stat-item{{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:10px 16px;text-align:center;}}
.stat-val{{font-size:20px;font-weight:800;margin-bottom:2px;}}
.stat-key{{font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace;text-transform:uppercase;letter-spacing:1px;}}
.footer{{text-align:center;padding:20px;color:var(--muted);font-size:11px;font-family:'JetBrains Mono',monospace;}}
::-webkit-scrollbar{{width:5px;}}::-webkit-scrollbar-track{{background:var(--surface);}}::-webkit-scrollbar-thumb{{background:var(--border);border-radius:3px;}}
</style>
</head>
<body>
<div class="app">
  <div class="header">
    <div class="badge">NEET Quiz</div>
    <h1>{safe_title}</h1>
    <p>// attempt karo — har question ka jawab do</p>
  </div>
  <div class="panel" id="quiz-panel">
    <div class="quiz-meta">
      <div>
        <div style="font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--accent);margin-bottom:3px;font-family:'JetBrains Mono',monospace;">{safe_title}</div>
        <div style="font-size:12px;color:var(--muted);font-family:'JetBrains Mono',monospace;">Q<span id="cur-num">1</span> of <span id="tot-num">?</span></div>
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <div class="meta-badge">📚 {len(questions)} Qs</div>
        <button class="btn btn-outline" onclick="restartQuiz()" style="padding:5px 12px;font-size:12px;">↺ Restart</button>
      </div>
    </div>
    <div class="progress-bar"><div class="progress-fill" id="prog-fill"></div></div>
    <div id="q-area"></div>
    <div class="nav-row">
      <button class="btn btn-outline" id="prev-btn" onclick="nav(-1)">← Prev</button>
      <button class="btn btn-outline" id="skip-btn" onclick="skipQ()">Skip →</button>
      <button class="btn" id="next-btn" style="display:none" onclick="nav(1)">Next →</button>
      <button class="btn btn-success" id="fin-btn" style="display:none" onclick="finishQuiz()">Finish ✓</button>
    </div>
  </div>
  <div class="panel" id="score-card">
    <div class="score-ring">
      <svg viewBox="0 0 110 110" width="130" height="130">
        <circle class="ring-bg" cx="55" cy="55" r="50"></circle>
        <circle class="ring-fill" id="ring-fill" cx="55" cy="55" r="50"></circle>
        <defs><linearGradient id="grad" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" style="stop-color:#7c6af7"/><stop offset="100%" style="stop-color:#6af7c7"/>
        </linearGradient></defs>
      </svg>
      <div class="score-center">
        <div class="big-score" id="score-pct">0%</div>
        <div class="score-label">SCORE</div>
      </div>
    </div>
    <div class="grade-badge" id="grade-txt"></div>
    <div style="color:var(--muted);font-size:13px;margin-bottom:12px;" id="grade-msg"></div>
    <div class="score-stats">
      <div class="stat-item"><div class="stat-val" style="color:var(--success)" id="s-cor">0</div><div class="stat-key">Correct</div></div>
      <div class="stat-item"><div class="stat-val" style="color:var(--danger)" id="s-wrg">0</div><div class="stat-key">Wrong</div></div>
      <div class="stat-item"><div class="stat-val" style="color:var(--muted)" id="s-skp">0</div><div class="stat-key">Skipped</div></div>
    </div>
    <button class="btn btn-outline" onclick="restartQuiz()" style="margin-top:14px;">↺ Try Again</button>
  </div>
  <div class="footer">Made with ❤️ by {CREDIT_TAG} | {len(questions)} Questions</div>
</div>
<script>
const LETTERS=['A','B','C','D','E','F'];
const questions={q_json};
let userAns={{}},curIdx=0;
document.getElementById('tot-num').textContent=questions.length;
renderQ();
function renderQ(){{
  const q=questions[curIdx],done=userAns[curIdx]!==undefined,isLast=curIdx===questions.length-1;
  document.getElementById('cur-num').textContent=curIdx+1;
  document.getElementById('prog-fill').style.width=((curIdx+1)/questions.length*100)+'%';
  document.getElementById('prev-btn').disabled=curIdx===0;
  document.getElementById('skip-btn').style.display=!done?'block':'none';
  document.getElementById('next-btn').style.display=done&&!isLast?'block':'none';
  document.getElementById('fin-btn').style.display=done&&isLast?'block':'none';
  const optHtml=q.opts.map((opt,i)=>{{
    let cls='opt-btn';
    if(done){{if(i===q.ans)cls+=' correct';else if(i===userAns[curIdx])cls+=' wrong';}}
    return '<button class="'+cls+'" onclick="pick('+i+')" '+(done?'disabled':'')+'>'+
      '<span class="opt-letter">'+LETTERS[i]+'</span><span>'+opt+'</span></button>';
  }}).join('');
  const ex=done?'<div class="explain-box">✓ Sahi Jawab: <strong style="color:#fff">'+LETTERS[q.ans]+'. '+q.opts[q.ans]+'</strong></div>':'';
  document.getElementById('q-area').innerHTML='<div class="question-card"><div class="q-header"><span class="q-num">Q'+(curIdx+1)+'</span><div class="q-text">'+q.q+'</div></div><div class="options-grid">'+optHtml+'</div>'+ex+'</div>';
}}
function pick(i){{userAns[curIdx]=i;renderQ();}}
function nav(d){{const n=curIdx+d;if(n>=0&&n<questions.length){{curIdx=n;renderQ();}}}}
function skipQ(){{if(userAns[curIdx]===undefined)userAns[curIdx]=-1;curIdx<questions.length-1?++curIdx&&renderQ():finishQuiz();}}
function restartQuiz(){{userAns={{}};curIdx=0;document.getElementById('score-card').style.display='none';document.getElementById('quiz-panel').style.display='block';renderQ();}}
function finishQuiz(){{
  for(let i=0;i<questions.length;i++)if(userAns[i]===undefined)userAns[i]=-1;
  let cor=0,wrg=0,skp=0;
  questions.forEach((q,i)=>{{const a=userAns[i];a===-1?skp++:a===q.ans?cor++:wrg++;}});
  const pct=Math.round(cor/questions.length*100);
  const [,gt,gm]=[[90,'🏆 Excellent!','Outstanding!'],[75,'🎯 Great!','Well done!'],[60,'👍 Good','Keep practicing!'],[40,'📖 Fair','Review topics.'],[0,'💪 Keep Going!','Practice more!']].find(([m])=>pct>=m);
  document.getElementById('quiz-panel').style.display='none';
  document.getElementById('score-card').style.display='block';
  document.getElementById('score-pct').textContent=pct+'%';
  document.getElementById('grade-txt').textContent=gt;
  document.getElementById('grade-msg').textContent=gm;
  document.getElementById('s-cor').textContent=cor;
  document.getElementById('s-wrg').textContent=wrg;
  document.getElementById('s-skp').textContent=skp;
  const c=2*Math.PI*50,fill=pct/100*c,ring=document.getElementById('ring-fill');
  ring.style.stroke=pct>=75?'url(#grad)':pct>=50?'var(--accent2)':'var(--danger)';
  setTimeout(()=>ring.style.strokeDasharray=fill+' '+c,100);
  document.getElementById('score-card').scrollIntoView({{behavior:'smooth'}});
}}
</script>
</body>
</html>"""

# ══════════════════════════════════════════════════════════════════════════════
#  OWNER COMMANDS
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid): await update.message.reply_text("❌ Sirf owner."); return
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage:\n`/adduser 123456789` — permanent\n`/adduser 123456789 30` — 30 din ke liye", parse_mode=ParseMode.MARKDOWN); return
    try:
        new_uid = int(args[0])
        if new_uid in OWNER_IDS: await update.message.reply_text("⚠️ Yeh owner hai!"); return
        users = load_auth_users()
        days = int(args[1]) if len(args)>1 else None
        if days:
            expiry = (datetime.now() + timedelta(days=days)).isoformat()
            users[str(new_uid)] = expiry
            await update.message.reply_text(f"✅ User `{new_uid}` add kiya!\n📅 Expiry: *{days} din* ({expiry[:10]})", parse_mode=ParseMode.MARKDOWN)
        else:
            users[str(new_uid)] = None
            await update.message.reply_text(f"✅ User `{new_uid}` add kiya! *(Permanent)*", parse_mode=ParseMode.MARKDOWN)
        save_auth_users(users)
    except ValueError:
        await update.message.reply_text("❌ Valid ID aur days do.")

async def cmd_removeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid): await update.message.reply_text("❌ Sirf owner."); return
    if not ctx.args: await update.message.reply_text("Usage: `/removeuser 123456789`", parse_mode=ParseMode.MARKDOWN); return
    try:
        rem_uid = int(ctx.args[0])
        if rem_uid in OWNER_IDS: await update.message.reply_text("❌ Owner remove nahi hoga!"); return
        users = load_auth_users()
        if str(rem_uid) not in users: await update.message.reply_text("⚠️ User list mein nahi."); return
        del users[str(rem_uid)]
        save_auth_users(users)
        await update.message.reply_text(f"✅ User `{rem_uid}` remove ho gaya!", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("❌ Valid ID do.")

async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid): await update.message.reply_text("❌ Sirf owner."); return
    users = load_auth_users()
    now = datetime.now()
    owner_list = "\n".join(f"👑 `{o}`" for o in OWNER_IDS)
    lines = []
    for u, exp in users.items():
        if exp is None:
            lines.append(f"✅ `{u}` — Permanent")
        else:
            exp_dt = datetime.fromisoformat(exp)
            remaining = (exp_dt - now).days
            if remaining > 0:
                lines.append(f"⏳ `{u}` — {remaining} din baki ({exp[:10]})")
            else:
                lines.append(f"❌ `{u}` — Expired!")
    auth_list = "\n".join(lines) if lines else "_Koi nahi_"
    await update.message.reply_text(
        f"*👥 Bot Users*\n\n*Owners:*\n{owner_list}\n\n*Auth Users:*\n{auth_list}",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "User"
    await update.message.reply_text(
        f"👤 *{name}*\n🆔 Your ID: `{uid}`",
        parse_mode=ParseMode.MARKDOWN)

# ══════════════════════════════════════════════════════════════════════════════
#  /start — PRO VERSION 😤
# ══════════════════════════════════════════════════════════════════════════════
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid       = update.effective_user.id
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type
    name      = update.effective_user.first_name or "User"

    if not is_allowed_chat(uid, chat_id, chat_type):
        await update.message.reply_text("❌ Is group mein bot allowed nahi hai."); return

    if not is_authorized(uid):
        await update.message.reply_text(
            "╔══════════════════════╗\n"
            "║   🔐 ACCESS DENIED   ║\n"
            "╚══════════════════════╝\n\n"
            "Bhai yahan free entry nahi hai! 😤\n\n"
            "Owner se contact karo: @xdsonic\n"
            "Apna ID bhejo: /myid"
        ); return

    joined = is_owner(uid) or await check_force_join(ctx.bot, uid)
    if not joined:
        keyboard = [[InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCE_CHANNEL}")],
                    [InlineKeyboardButton("✅ Joined! Check Again", callback_data="check_join")]]
        await update.message.reply_text(
            f"⚠️ *Pehle channel join karo!*\n\n"
            f"👇 Join karo phir *Check Again* press karo.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)); return

    await show_main_menu(update, ctx, uid, name)

async def show_main_menu(update, ctx, uid, name):
    resume = load_resume()
    users  = load_auth_users()
    role   = "👑 Owner" if is_owner(uid) else "✅ Member"

    expiry_text = ""
    if not is_owner(uid) and str(uid) in users:
        exp = users[str(uid)]
        if exp:
            remaining = (datetime.fromisoformat(exp) - datetime.now()).days
            expiry_text = f"\n⏳ Access: *{remaining} din baki*"

    keyboard = [
        [InlineKeyboardButton("📥  PDF  Downloader", callback_data="mode_download")],
        [
            InlineKeyboardButton("📊 Quiz Poll",  callback_data="mode_quiz_poll"),
            InlineKeyboardButton("🌐 Quiz HTML",  callback_data="mode_quiz_html"),
        ],
        [InlineKeyboardButton("🧬 NEET Generator", callback_data="neet_start")],   # <-- NEW
    ]
    if str(uid) in resume:
        idx   = resume[str(uid)].get("current_index",0)
        total = len(resume[str(uid)].get("entries",[]))
        keyboard.append([InlineKeyboardButton(
            f"▶️ Resume Download (#{idx+1}, {total-idx} baki)",
            callback_data="resume")])

    msg = (
        f"⚡ *SONIC BOT*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 {name} | {role}{expiry_text}\n\n"
        f"Kya karna hai aaj? 👇"
    )
    reply = update.message if hasattr(update,'message') and update.message else None
    if reply:
        await reply.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

# ══════════════════════════════════════════════════════════════════════════════
#  BUTTON HANDLER (existing) — extended with NEET
# ══════════════════════════════════════════════════════════════════════════════
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid   = query.from_user.id
    await query.answer()

    if query.data == "check_join":
        joined = await check_force_join(ctx.bot, uid)
        if joined:
            name = query.from_user.first_name or "User"
            await query.message.delete()
            await show_main_menu_from_query(query, ctx, uid, name)
        else:
            await query.answer("❌ Abhi join nahi kiya! Pehle join karo.", show_alert=True)
        return

    if not is_authorized(uid):
        await query.answer("Unauthorized!", show_alert=True); return

    if query.data == "mode_download":
        user_mode[uid] = "download"
        user_sessions.pop(uid, None)
        await query.edit_message_text(
            "📥 *PDF Downloader*\n\nText file bhejo (PDF links).\n\n*Format:*\n`File Name:https://link.pdf`",
            parse_mode=ParseMode.MARKDOWN)

    elif query.data == "mode_quiz_poll":
        user_mode[uid] = "quiz_poll"
        mcq_buffer.pop(uid, None)
        msg_to_delete.pop(uid, None)
        await query.edit_message_text(
            "📊 *Quiz Poll Mode*\n\n"
            "MCQ paste karo → `/done` → Telegram Polls! 🎯\n\n"
            "*Format:*\n`Q1. Question`\n`(1) A`\n`(2) B`\n`(3) C`\n`(4) D`\n`ANS: 2`\n\n"
            "💡 *Tip:* Seedha *.txt file* bhi bhej sakte ho!\n"
            "_50-50 chunks mein paste karo, phir /done_",
            parse_mode=ParseMode.MARKDOWN)

    elif query.data == "mode_quiz_html":
        user_mode[uid] = "quiz_html"
        mcq_buffer.pop(uid, None)
        msg_to_delete.pop(uid, None)
        user_sessions[uid] = {"waiting_title": True}
        await query.edit_message_text(
            "🌐 *Quiz HTML Mode*\n\n"
            "Pehle *file ka naam* type karo 👇\n"
            "_(Yahi naam se HTML file save hogi!)_\n\n"
            "_Jaise: Motion-DPP-07 ya Biology-Ch3_\n\n"
            "💡 *Tip:* Naam dene ke baad .txt file seedha bhej sakte ho!",
            parse_mode=ParseMode.MARKDOWN)

    elif query.data == "resume":
        resume = load_resume()
        if str(uid) in resume:
            sess = resume[str(uid)]
            user_sessions[uid] = sess
            user_mode[uid] = "download"
            start_idx = sess.get("current_index",0)
            total     = len(sess.get("entries",[]))
            await query.edit_message_text(
                f"▶️ *Resuming from #{start_idx+1}*\n📄 Remaining: {total-start_idx} files",
                parse_mode=ParseMode.MARKDOWN)
            task = asyncio.create_task(run_downloads(uid,ctx,query.message.chat_id,start_idx))
            active_tasks[uid] = task

    # NEET flow starts via callback from main menu
    elif query.data == "neet_start":
        await start_neet_flow(query, ctx, uid)

async def show_main_menu_from_query(query, ctx, uid, name):
    resume = load_resume()
    users  = load_auth_users()
    role   = "👑 Owner" if is_owner(uid) else "✅ Member"
    expiry_text = ""
    if not is_owner(uid) and str(uid) in users:
        exp = users[str(uid)]
        if exp:
            remaining = (datetime.fromisoformat(exp) - datetime.now()).days
            expiry_text = f"\n⏳ Access: *{remaining} din baki*"
    keyboard = [
        [InlineKeyboardButton("📥  PDF  Downloader", callback_data="mode_download")],
        [InlineKeyboardButton("📊 Quiz Poll", callback_data="mode_quiz_poll"),
         InlineKeyboardButton("🌐 Quiz HTML", callback_data="mode_quiz_html")],
        [InlineKeyboardButton("🧬 NEET Generator", callback_data="neet_start")],
    ]
    if str(uid) in resume:
        idx   = resume[str(uid)].get("current_index",0)
        total = len(resume[str(uid)].get("entries",[]))
        keyboard.append([InlineKeyboardButton(f"▶️ Resume Download (#{idx+1}, {total-idx} baki)", callback_data="resume")])
    msg = (
        f"⚡ *SONIC BOT*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 {name} | {role}{expiry_text}\n\n"
        f"Kya karna hai aaj? 👇"
    )
    await ctx.bot.send_message(query.message.chat_id, msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

# ══════════════════════════════════════════════════════════════════════════════
#  NEET GENERATOR — New Feature (merged from app.py)
# ══════════════════════════════════════════════════════════════════════════════

# ─── NEET Config ────────────────────────────────────────────────────────────
NEET_API_KEYS = os.environ.get("NEET_API_KEYS", "").split(",") if os.environ.get("NEET_API_KEYS") else [
    "sk-nry-qmZ7wto1VLZxBQvR3ICu2uplL2OcIa-ByOOLW3Ccnos",
    "sk-nry-3APCsNy-QiZi3yJsVhTc2icHd4AEk8aFLcB4wm-qFtA",
    "sk-nry-dMEChsC_hFGFRXkaIisl1_e8pvRBb5JonwT_R55dtn8",
]
NEET_API_KEYS = [k for k in NEET_API_KEYS if k]
NEET_BASE_URL = "https://router.bynara.id/v1"
NEET_MODEL_POOL = [
    "mistral-large",
    "mistral-medium-3-5",
]
NEET_RATE_LIMIT_GAP = 6.5  # seconds
NEET_COOLDOWN_SECONDS = 120

# Internal state for API
_neet_key_index = 0
_neet_last_call = 0.0
_neet_model_cooldown = {}  # model -> timestamp

NEET_SYSTEM_PROMPT = """You are a senior NEET (NTA) question paper setter and examiner with 20+ years of experience.

YOUR MANDATE:
- Produce questions that are INDISTINGUISHABLE from real NEET (NTA) exam papers
- Match the actual NEET difficulty curve
- Base every fact strictly on NCERT (Class 11 & 12)
- NEVER write simple, generic, "AI-quiz-style" questions
- NEVER start a question by literally repeating the topic name
- At least 2 out of every 4 options must be genuinely plausible distractors

QUESTION STYLE MIX:
1. Assertion–Reason
2. Statement-based
3. Match the Following
4. Multi-concept
5. Diagram-based described in text
6. Application/case-based
7. Exception-based
8. Conceptual-trap
9. Direct PYQ-style factual/conceptual recall

HARD RULES:
1. Return ONLY a valid JSON array. No markdown. No ```json fences.
2. Exact format: {"question":"...","options":["A) text","B) text","C) text","D) text"],"correct":0,"explanation":"...","ncert_ref":"Class XI/XII - Chapter N: Chapter Name","difficulty":"Easy/Medium/Hard"}
3. "correct" is a 0-based index (0=A,1=B,2=C,3=D).
4. All 4 options must be distinct and plausible.
5. "explanation" must state WHY correct option is correct AND WHY each other is wrong.
6. "ncert_ref" must be a real, accurate NCERT chapter/topic reference.
7. NEVER repeat the same underlying concept twice.
8. If language is Gujarati or Hindi, ENTIRE JSON must be in that language.
9. Output must start with [ and end with ] — absolutely nothing else."""

GUJARATI_GLOSSARY = """Reference Gujarati scientific terms:
કોષ (Cell), કોષકેન્દ્ર (Nucleus), ઉત્સેચક (Enzyme), વર્ણસૂત્ર (Chromosome), અનુવંશિકતા (Heredity),
પ્રકાશસંશ્લેષણ (Photosynthesis), શ્વસન (Respiration), હોર્મોન (Hormone), પેશી (Tissue), અંગ (Organ),
DNA-આરએનએ (DNA-RNA), ઉત્ક્રાંતિ (Evolution), પ્રજનન (Reproduction), ચેતાતંત્ર (Nervous system),
પરિવહન (Transport), ઉત્સર્જન (Excretion), પ્રતિરક્ષાતંત્ર (Immune system), જનીન (Gene), પ્રોટીન (Protein),
કાર્બોદિત (Carbohydrate)."""

LANG_CHUNK_CAP = {"english": 10, "hindi": 6, "gujarati": 5}
LANG_TIMEOUT = {"english": 60.0, "hindi": 90.0, "gujarati": 120.0}
LANG_TEMPERATURE = {"english": 0.4, "hindi": 0.3, "gujarati": 0.25}
LANG_TOKENS_PER_Q = {"english": 250, "hindi": 500, "gujarati": 600}

# ─── NEET Helper Functions ──────────────────────────────────────────────────
def _neet_next_key():
    global _neet_key_index
    if not NEET_API_KEYS:
        raise RuntimeError("No NEET API keys configured.")
    key = NEET_API_KEYS[_neet_key_index % len(NEET_API_KEYS)]
    _neet_key_index += 1
    return key

def _neet_rate_limit_wait():
    global _neet_last_call
    now = time.time()
    wait = NEET_RATE_LIMIT_GAP - (now - _neet_last_call)
    if wait > 0:
        time.sleep(wait)
    _neet_last_call = time.time()

def _neet_mark_model_down(model):
    _neet_model_cooldown[model] = time.time() + NEET_COOLDOWN_SECONDS

def _neet_is_model_cooling(model):
    return time.time() < _neet_model_cooldown.get(model, 0)

def _neet_tokens_for(language, count):
    per_q = LANG_TOKENS_PER_Q.get(language, LANG_TOKENS_PER_Q["english"])
    return min(8000, int(count * per_q) + 400)

def _neet_chunk_cap(language):
    return LANG_CHUNK_CAP.get(language, LANG_CHUNK_CAP["english"])

def _neet_call_api_sync(messages, max_tokens=4000, temperature=0.4, timeout=120.0):
    """Synchronous API call with retries and model fallback."""
    if not NEET_API_KEYS:
        raise RuntimeError("No API keys configured.")
    if not NEET_MODEL_POOL:
        raise RuntimeError("No models configured.")

    last_error = None
    attempts_per_model = 2
    backoff = [3, 8]

    healthy = [m for m in NEET_MODEL_POOL if not _neet_is_model_cooling(m)]
    cooling = [m for m in NEET_MODEL_POOL if _neet_is_model_cooling(m)]
    ordered_models = healthy + cooling

    for model_name in ordered_models:
        for attempt in range(attempts_per_model):
            key = _neet_next_key()
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            payload = {
                "model": model_name,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature
            }
            try:
                _neet_rate_limit_wait()
                with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
                    r = client.post(f"{NEET_BASE_URL}/chat/completions", headers=headers, json=payload)
                if r.status_code == 401:
                    logger.error(f"[NEET] {model_name} 401 Unauthorized")
                    last_error = Exception(f"{model_name}: 401")
                    time.sleep(1)
                    continue
                if r.status_code == 429:
                    logger.warning(f"[NEET] {model_name} rate-limited")
                    last_error = Exception(f"{model_name}: rate limited")
                    time.sleep(backoff[min(attempt, len(backoff)-1)])
                    continue
                if r.status_code >= 500:
                    logger.warning(f"[NEET] {model_name} server error {r.status_code}")
                    last_error = Exception(f"{model_name}: server error")
                    time.sleep(backoff[min(attempt, len(backoff)-1)])
                    continue
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                if content and content.strip():
                    return content
                last_error = Exception(f"{model_name}: empty response")
                time.sleep(1.5)
            except Exception as e:
                logger.error(f"[NEET] {model_name} attempt {attempt+1} failed: {e}")
                last_error = e
                time.sleep(backoff[min(attempt, len(backoff)-1)])
        _neet_mark_model_down(model_name)
        logger.warning(f"[NEET] Model '{model_name}' cooling down for {NEET_COOLDOWN_SECONDS}s")

    raise last_error or RuntimeError("All NEET models/keys exhausted.")

def _neet_strip_json(raw):
    if not raw: return None
    raw = raw.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    s = raw.find("[")
    e = raw.rfind("]")
    if s == -1 or e == -1 or e < s: return None
    return raw[s:e+1]

def _neet_fix_json(json_str):
    json_str = re.sub(r',\s*([\]}])', r'\1', json_str)
    json_str = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', ' ', json_str)
    json_str = re.sub(r',\s*,', ',', json_str)
    return json_str

def _neet_partial_recover(json_str):
    objects = []
    depth = 0
    start = None
    for i, ch in enumerate(json_str):
        if ch == '{':
            if depth == 0: start = i
            depth += 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    chunk = json_str[start:i+1]
                    try:
                        obj = json.loads(_neet_fix_json(chunk))
                        objects.append(obj)
                    except Exception:
                        pass
                    start = None
    return objects

def _neet_parse_mcq(raw, level):
    if not raw: return []
    json_str = _neet_strip_json(raw)
    parsed = None
    if json_str:
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            fixed = _neet_fix_json(json_str)
            try:
                parsed = json.loads(fixed)
            except json.JSONDecodeError:
                parsed = _neet_partial_recover(fixed)
    if not parsed:
        parsed = _neet_partial_recover(_neet_fix_json(raw))
    if not isinstance(parsed, list):
        return []
    valid = []
    for q in parsed:
        if not isinstance(q, dict): continue
        q_text = str(q.get("question", "")).strip()
        if not q_text or len(q_text) < 5: continue
        opts = q.get("options", [])
        if not isinstance(opts, list) or len(opts) < 4: continue
        correct_raw = q.get("correct", 0)
        try: correct_idx = int(correct_raw)
        except: correct_idx = 0
        if correct_idx not in (0,1,2,3): correct_idx = 0
        valid.append({
            "question": q_text,
            "options": [str(o) for o in opts[:4]],
            "correct": correct_idx,
            "explanation": str(q.get("explanation", "Refer NCERT.")),
            "ncert_ref": str(q.get("ncert_ref", "")),
            "difficulty": str(q.get("difficulty", level.title())),
        })
    return valid

def _neet_translate_gujarati(questions):
    if not questions: return []
    payload = json.dumps(questions, ensure_ascii=False)
    prompt = f"""Translate the following NEET MCQ JSON array into natural, fluent GUJARATI (ગુજરાતી). Do NOT use Hindi or English.

{GUJARATI_GLOSSARY}

Keep exact same JSON structure, same "correct" index.

Input JSON:
{payload}

Return ONLY translated JSON array. No markdown."""
    messages = [
        {"role": "system", "content": "You are a professional Gujarati science translator. Return only valid JSON."},
        {"role": "user", "content": prompt}
    ]
    for attempt in range(3):
        try:
            raw = _neet_call_api_sync(messages, max_tokens=_neet_tokens_for("gujarati", len(questions)), temperature=0.25, timeout=120.0)
            result = _neet_parse_mcq(raw, "medium")
            if result and len(result) >= max(1, len(questions)//2):
                return result
        except Exception as e:
            logger.error(f"Gujarati translation attempt {attempt+1} failed: {e}")
            time.sleep(2)
    return []

def _neet_translate_hindi(questions):
    if not questions: return []
    payload = json.dumps(questions, ensure_ascii=False)
    prompt = f"""Translate the following NEET MCQ JSON array into natural, fluent HINDI (हिंदी).

Keep exact same JSON structure, same "correct" index.

Input JSON:
{payload}

Return ONLY translated JSON array. No markdown."""
    messages = [
        {"role": "system", "content": "You are a professional Hindi science translator. Return only valid JSON."},
        {"role": "user", "content": prompt}
    ]
    for attempt in range(3):
        try:
            raw = _neet_call_api_sync(messages, max_tokens=_neet_tokens_for("hindi", len(questions)), temperature=0.25, timeout=90.0)
            result = _neet_parse_mcq(raw, "medium")
            if result and len(result) >= max(1, len(questions)//2):
                return result
        except Exception as e:
            logger.error(f"Hindi translation attempt {attempt+1} failed: {e}")
            time.sleep(2)
    return []

def _neet_generate_single_batch(topic, level, count, language, start_num, total, avoid_concepts=None):
    level_map = {
        "easy": "Easy (NCERT direct)",
        "medium": "Medium (conceptual)",
        "hard": "Hard (tricky)",
        "mixed": "Mixed — variety"
    }
    if language == "gujarati":
        lang_instruction = (
            "Write ENTIRE output in natural, fluent GUJARATI (ગુજરાતી). "
            "Use correct scientific terminology. Do NOT use Hindi or English.\n"
            + GUJARATI_GLOSSARY
        )
    elif language == "hindi":
        lang_instruction = "Write ENTIRE output in pure, natural Hindi (हिंदी)."
    else:
        lang_instruction = "Write everything in clear, formal exam English."

    avoid_block = ""
    if avoid_concepts:
        avoid_list = "\n".join(f"- {c}" for c in list(avoid_concepts)[-50:])
        avoid_block = f"\nConcepts ALREADY COVERED - do NOT repeat:\n{avoid_list}\n"

    prompt = f"""Generate exactly {count} NEET-level MCQ questions covering: "{topic}"

Difficulty: {level_map.get(level, level)}
{lang_instruction}

Questions {start_num} to {start_num + count - 1} of {total}
{avoid_block}

Return ONLY JSON array. Start with [ end with ]"""

    messages = [
        {"role": "system", "content": NEET_SYSTEM_PROMPT},
        {"role": "user", "content": prompt}
    ]

    max_tokens = _neet_tokens_for(language, count)
    temperature = LANG_TEMPERATURE.get(language, 0.4)
    timeout = LANG_TIMEOUT.get(language, 60.0)

    max_retries = 5
    for attempt in range(max_retries):
        try:
            raw = _neet_call_api_sync(messages, max_tokens=max_tokens, temperature=temperature, timeout=timeout)
            result = _neet_parse_mcq(raw, level)
            if result and len(result) >= count:
                return result[:count]
            if result and attempt >= max_retries - 2:
                return result
        except Exception as e:
            logger.error(f"{language} attempt {attempt+1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(3)

    # Fallback: generate English then translate
    if language in ("gujarati", "hindi"):
        logger.warning(f"Direct {language} failed — trying English-first fallback")
        try:
            en_prompt = prompt.replace(lang_instruction, "Write everything in clear, formal exam English.")
            en_messages = [
                {"role": "system", "content": NEET_SYSTEM_PROMPT},
                {"role": "user", "content": en_prompt}
            ]
            raw_en = _neet_call_api_sync(en_messages, max_tokens=_neet_tokens_for("english", count), temperature=0.4, timeout=60.0)
            en_questions = _neet_parse_mcq(raw_en, level)
            if en_questions:
                if language == "gujarati":
                    translated = _neet_translate_gujarati(en_questions)
                else:
                    translated = _neet_translate_hindi(en_questions)
                if translated:
                    return translated[:count]
        except Exception as e:
            logger.error(f"Fallback failed: {e}")
    return []

def _neet_generate_batch(topic, level, count, language, start_num, total, avoid_concepts=None):
    chunk_cap = _neet_chunk_cap(language)
    if language in ("gujarati", "hindi") and count > chunk_cap:
        results = []
        seen_local = set()
        remaining = count
        cur_start = start_num
        avoid_running = list(avoid_concepts) if avoid_concepts else []
        max_loops = 20
        while remaining > 0 and len(results) < count and max_loops > 0:
            max_loops -= 1
            take = min(chunk_cap, remaining)
            sub = _neet_generate_single_batch(topic, level, take, language, cur_start, total, avoid_concepts=avoid_running)
            for q in sub:
                key = q["question"][:60].lower().strip()
                if key in seen_local: continue
                seen_local.add(key)
                results.append(q)
                avoid_running.append(" ".join(q["question"].split()[:12]))
            remaining = count - len(results)
            cur_start = start_num + len(results)
            if remaining > 0:
                time.sleep(NEET_RATE_LIMIT_GAP)
        return results
    return _neet_generate_single_batch(topic, level, count, language, start_num, total, avoid_concepts)

def _neet_generate_all_mcqs(topic, level, count, language, progress_callback=None):
    if language == "gujarati":
        BATCH_SIZE = 5
    elif language == "hindi":
        BATCH_SIZE = 6
    else:
        BATCH_SIZE = 10

    all_questions = []
    seen_keys = set()
    covered_concepts = []
    total_batches = (count + BATCH_SIZE - 1) // BATCH_SIZE
    MAX_TOPUP_ROUNDS = 5 if language in ("gujarati", "hindi") else 3

    for batch_num in range(total_batches):
        remaining_overall = count - len(all_questions)
        if remaining_overall <= 0: break
        batch_target = min(BATCH_SIZE, remaining_overall)
        start_num = len(all_questions) + 1

        batch_questions = []
        batch_seen = set()
        needed = batch_target

        for round_no in range(MAX_TOPUP_ROUNDS + 1):
            if needed <= 0: break
            chunk = _neet_generate_batch(
                topic, level, needed, language, start_num, count,
                avoid_concepts=covered_concepts
            )
            for q in chunk:
                key = q["question"][:60].lower().strip()
                if key in seen_keys or key in batch_seen:
                    continue
                seen_keys.add(key)
                batch_seen.add(key)
                batch_questions.append(q)
                covered_concepts.append(" ".join(q["question"].split()[:12]))
            needed = batch_target - len(batch_questions)
            if needed > 0 and round_no < MAX_TOPUP_ROUNDS:
                logger.warning(f"Batch {batch_num+1}: short by {needed}, topping up...")
                time.sleep(1)

        for q in batch_questions:
            q["num"] = len(all_questions) + 1
            all_questions.append(q)

        if progress_callback:
            progress_callback(len(all_questions), count, batch_num + 1, total_batches)

        if batch_num < total_batches - 1:
            time.sleep(1)

    for i, q in enumerate(all_questions):
        q["num"] = i + 1
    return all_questions

# ─── NEET HTML Template (AI version) ──────────────────────────────────────
NEET_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="{language}">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} - {topic}</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        *{{margin:0;padding:0;box-sizing:border-box}}
        body{{font-family:'Inter',sans-serif;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);min-height:100vh;padding:20px}}
        .container{{max-width:900px;margin:0 auto;background:rgba(255,255,255,0.95);backdrop-filter:blur(10px);border-radius:30px;padding:40px;box-shadow:0 20px 60px rgba(0,0,0,0.3)}}
        .header{{text-align:center;margin-bottom:35px;padding-bottom:25px;border-bottom:3px solid #667eea}}
        .header h1{{font-size:2.5rem;font-weight:800;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:8px}}
        .header .meta{{color:#555;font-size:.95rem;display:flex;justify-content:center;gap:25px;flex-wrap:wrap;margin-top:10px}}
        .header .meta span{{background:#f0f0f0;padding:4px 16px;border-radius:20px;font-weight:500}}
        .progress-bar{{width:100%;height:6px;background:#e0e0e0;border-radius:10px;margin-bottom:30px;overflow:hidden}}
        .progress-bar .progress{{height:100%;background:linear-gradient(90deg,#667eea,#764ba2);border-radius:10px;transition:width .3s ease;width:0%}}
        .question-card{{background:#f8f9ff;border-radius:20px;padding:28px;margin-bottom:25px;border-left:5px solid #667eea;transition:all .3s ease;opacity:0;transform:translateY(20px);animation:fadeInUp .5s forwards}}
        .question-card:nth-child(2){{animation-delay:.1s}}
        .question-card:nth-child(3){{animation-delay:.2s}}
        .question-card:nth-child(4){{animation-delay:.3s}}
        .question-card:nth-child(5){{animation-delay:.4s}}
        @keyframes fadeInUp{{to{{opacity:1;transform:translateY(0)}}}}
        .question-number{{display:inline-block;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;padding:2px 14px;border-radius:20px;font-size:.8rem;font-weight:700;margin-bottom:12px}}
        .question-text{{font-size:1.15rem;font-weight:600;color:#1a1a2e;margin-bottom:18px;line-height:1.6}}
        .options{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
        @media(max-width:600px){{.options{{grid-template-columns:1fr}}}}
        .option{{background:#fff;border:2px solid #e0e0e0;border-radius:12px;padding:14px 18px;cursor:pointer;transition:all .3s ease;display:flex;align-items:center;gap:12px;font-weight:500;color:#333}}
        .option:hover{{border-color:#667eea;background:#f0f2ff;transform:translateY(-2px);box-shadow:0 4px 15px rgba(102,126,234,.15)}}
        .option .letter{{background:#667eea;color:#fff;width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:.85rem;flex-shrink:0}}
        .option.selected{{border-color:#667eea;background:#eef0ff}}
        .option.correct{{border-color:#10b981;background:#d1fae5}}
        .option.correct .letter{{background:#10b981}}
        .option.wrong{{border-color:#ef4444;background:#fee2e2}}
        .option.wrong .letter{{background:#ef4444}}
        .option.disabled{{cursor:default;opacity:.8}}
        .option input[type="radio"]{{display:none}}
        .btn{{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;border:none;padding:16px 40px;border-radius:16px;font-size:1.1rem;font-weight:700;cursor:pointer;transition:all .3s ease;display:inline-block;text-align:center;width:100%;max-width:300px;margin:10px auto}}
        .btn:hover{{transform:translateY(-3px);box-shadow:0 10px 30px rgba(102,126,234,.4)}}
        .btn:disabled{{opacity:.5;cursor:not-allowed;transform:none}}
        .btn-secondary{{background:#6b7280}}
        .btn-secondary:hover{{box-shadow:0 10px 30px rgba(107,114,128,.4)}}
        .btn-success{{background:linear-gradient(135deg,#10b981,#059669)}}
        .btn-success:hover{{box-shadow:0 10px 30px rgba(16,185,129,.4)}}
        .button-group{{display:flex;flex-direction:column;align-items:center;gap:12px;margin-top:30px;padding-top:25px;border-top:2px solid #e0e0e0}}
        .button-group .row{{display:flex;gap:15px;flex-wrap:wrap;justify-content:center;width:100%}}
        .button-group .row .btn{{max-width:200px}}
        .result-box{{display:none;background:linear-gradient(135deg,#f0f4ff,#e8ecff);border-radius:20px;padding:30px;margin-top:30px;text-align:center;border:2px solid #667eea}}
        .result-box.show{{display:block;animation:fadeInUp .5s ease}}
        .result-box .score{{font-size:4rem;font-weight:800;color:#667eea}}
        .result-box .details{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:15px;margin:20px 0}}
        .result-box .details .stat{{background:#fff;padding:15px;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.05)}}
        .result-box .details .stat .num{{font-size:1.8rem;font-weight:700}}
        .result-box .details .stat .label{{color:#666;font-size:.85rem;margin-top:4px}}
        .stat-correct .num{{color:#10b981}}
        .stat-wrong .num{{color:#ef4444}}
        .stat-skipped .num{{color:#f59e0b}}
        .stat-score .num{{color:#667eea}}
        .result-box .grade{{font-size:1.3rem;font-weight:600;margin-top:15px;color:#1a1a2e}}
        .result-box .grade .emoji{{font-size:2rem}}
        .explanation{{margin-top:15px;padding:15px;background:#fff;border-radius:12px;border-left:4px solid #667eea;font-size:.95rem;color:#444;line-height:1.6}}
        .explanation .ncert{{display:inline-block;background:#eef0ff;padding:2px 12px;border-radius:12px;font-size:.8rem;font-weight:500;color:#667eea;margin-top:8px}}
        .footer{{text-align:center;margin-top:30px;padding-top:20px;border-top:2px solid #e0e0e0;color:#888;font-size:.85rem}}
        .hidden{{display:none!important}}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🧬 {title}</h1>
            <div class="meta">
                <span>📚 {topic}</span>
                <span>🎯 {level}</span>
                <span>📝 {count} {q_label}</span>
                <span>🌐 {language}</span>
            </div>
            <div class="progress-bar">
                <div class="progress" id="progressBar"></div>
            </div>
        </div>
        <div id="questionsContainer"></div>
        <div class="button-group" id="buttonGroup">
            <div class="row">
                <button class="btn" id="submitBtn" onclick="submitQuiz()">{submit_label}</button>
                <button class="btn btn-secondary" id="resetBtn" onclick="resetQuiz()">🔄 Reset</button>
            </div>
        </div>
        <div class="result-box" id="resultBox">
            <div class="score" id="scoreDisplay">0/0</div>
            <div class="details">
                <div class="stat stat-correct"><div class="num" id="correctCount">0</div><div class="label">✅ Correct</div></div>
                <div class="stat stat-wrong"><div class="num" id="wrongCount">0</div><div class="label">❌ Wrong</div></div>
                <div class="stat stat-skipped"><div class="num" id="skippedCount">0</div><div class="label">⏭️ Skipped</div></div>
                <div class="stat stat-score"><div class="num" id="neetScore">0</div><div class="label">🎯 NEET Score</div></div>
            </div>
            <div class="grade" id="gradeDisplay"></div>
        </div>
        <div class="footer"><span>🧬 NEET MCQ Generator • NCERT Based • {language}</span></div>
    </div>
    <script>
        const questionsData = {questions_json};
        const langData = {lang_json};
        let userAnswers = {{}};
        let quizSubmitted = false;
        function renderQuestions() {{
            const container = document.getElementById('questionsContainer');
            container.innerHTML = '';
            const letters = ['A','B','C','D'];
            questionsData.forEach((q,index)=>{{
                const card = document.createElement('div');
                card.className = 'question-card';
                card.id = `q${{index}}`;
                let optionsHTML = '';
                q.options.forEach((opt,optIndex)=>{{
                    optionsHTML += `<label class="option" id="opt_${{index}}_${{optIndex}}" onclick="selectOption(${{index}},${{optIndex}})"><input type="radio" name="q${{index}}" value="${{optIndex}}"><span class="letter">${{letters[optIndex]}}</span><span>${{opt.replace(/^[A-D]\\)\\s*/,'')}}</span></label>`;
                }});
                card.innerHTML = `<span class="question-number">Q${{index+1}}</span><div class="question-text">${{q.question}}</div><div class="options">${{optionsHTML}}</div><div class="explanation hidden" id="exp_${{index}}">💡 <strong>Explanation:</strong> <span id="expText_${{index}}"></span><div class="ncert" id="ncertRef_${{index}}"></div></div>`;
                container.appendChild(card);
            }});
            updateProgress();
        }}
        function selectOption(qIndex,optIndex){{
            if(quizSubmitted) return;
            document.querySelectorAll(`input[name="q${{qIndex}}"]`).forEach(r=>r.checked=false);
            document.querySelectorAll(`#q${{qIndex}} .option`).forEach(el=>el.classList.remove('selected'));
            document.getElementById(`opt_${{qIndex}}_${{optIndex}}`).classList.add('selected');
            document.querySelector(`input[name="q${{qIndex}}"][value="${{optIndex}}"]`).checked=true;
            userAnswers[qIndex]=optIndex;
            updateProgress();
        }}
        function updateProgress(){{
            const total=questionsData.length, answered=Object.keys(userAnswers).length;
            document.getElementById('progressBar').style.width=(answered/total*100)+'%';
        }}
        function submitQuiz(){{
            if(quizSubmitted) return;
            const total=questionsData.length, answered=Object.keys(userAnswers).length;
            if(answered<total && !confirm(`⚠️ Aapne ${{total-answered}} questions skip kiye hain. Kya aap submit karna chahte hain?`)) return;
            quizSubmitted=true;
            document.getElementById('submitBtn').disabled=true;
            let correct=0,wrong=0,skipped=0;
            const letters=['A','B','C','D'];
            questionsData.forEach((q,index)=>{{
                const userAns=userAnswers[index], correctAns=q.correct;
                const expDiv=document.getElementById(`exp_${{index}}`);
                document.getElementById(`expText_${{index}}`).textContent=q.explanation||'Refer NCERT for details.';
                document.getElementById(`ncertRef_${{index}}`).textContent=q.ncert_ref||'NCERT Reference';
                expDiv.classList.remove('hidden');
                document.querySelectorAll(`#q${{index}} .option`).forEach((opt,optIndex)=>{{
                    opt.classList.add('disabled');
                    if(optIndex===correctAns) opt.classList.add('correct');
                    if(userAns===optIndex && userAns!==correctAns) opt.classList.add('wrong');
                }});
                if(userAns===undefined) skipped++;
                else if(userAns===correctAns) correct++;
                else wrong++;
            }});
            const neetScore=(correct*4)-(wrong*1), pct=(correct/total*100);
            let grade='', emoji='';
            if(pct>=80){{grade='🏆 Excellent! NEET Ready!';emoji='🌟';}}
            else if(pct>=60){{grade='👍 Good! Thoda aur practice karo';emoji='💪';}}
            else if(pct>=40){{grade='📚 Average — NCERT dobara padho';emoji='📖';}}
            else{{grade='💪 Keep Going — Practice makes perfect!';emoji='🔥';}}
            document.getElementById('correctCount').textContent=correct;
            document.getElementById('wrongCount').textContent=wrong;
            document.getElementById('skippedCount').textContent=skipped;
            document.getElementById('neetScore').textContent=neetScore;
            document.getElementById('scoreDisplay').textContent=`${{correct}}/${{total}}`;
            document.getElementById('gradeDisplay').innerHTML=`<span class="emoji">${{emoji}}</span> ${{grade}}`;
            document.getElementById('resultBox').classList.add('show');
            document.getElementById('resultBox').scrollIntoView({{behavior:'smooth'}});
        }}
        function resetQuiz(){{
            if(!confirm('🔄 Kya aap quiz reset karna chahte hain?')) return;
            userAnswers={{}}; quizSubmitted=false;
            document.getElementById('submitBtn').disabled=false;
            document.getElementById('resultBox').classList.remove('show');
            questionsData.forEach((q,index)=>{{
                document.getElementById(`exp_${{index}}`).classList.add('hidden');
                document.querySelectorAll(`#q${{index}} .option`).forEach(opt=>opt.classList.remove('selected','correct','wrong','disabled'));
                document.querySelectorAll(`input[name="q${{index}}"]`).forEach(r=>r.checked=false);
            }});
            updateProgress();
            window.scrollTo({{top:0,behavior:'smooth'}});
        }}
        renderQuestions();
    </script>
</body>
</html>"""

def _neet_generate_html_quiz(questions, topic, level, language, count):
    lang_map = {
        "english": {"title": "NEET Quiz", "q_label": "Questions", "submit_label": "Submit Quiz"},
        "hindi": {"title": "नीट क्विज", "q_label": "प्रश्न", "submit_label": "क्विज सबमिट करें"},
        "gujarati": {"title": "નીટ ક્વિઝ", "q_label": "પ્રશ્નો", "submit_label": "ક્વિઝ સબમિટ કરો"}
    }
    lang_data = lang_map.get(language, lang_map["english"])
    questions_json = json.dumps(questions, ensure_ascii=False)
    lang_json = json.dumps(lang_data, ensure_ascii=False)
    html = NEET_HTML_TEMPLATE.format(
        language=language.title(),
        title=lang_data["title"],
        topic=topic,
        level=level.title(),
        count=count,
        q_label=lang_data["q_label"],
        submit_label=lang_data["submit_label"],
        questions_json=questions_json,
        lang_json=lang_json
    )
    return html

# ─── NEET Flow ──────────────────────────────────────────────────────────────
neet_sessions = {}  # uid -> {"step": str, "level": str, "count": int, "language": str, "ready": bool, "chat_id": int}

def neet_keyboard_level():
    kb = [
        [InlineKeyboardButton("🟢 Easy", callback_data="neet_level_easy"),
         InlineKeyboardButton("🟡 Medium", callback_data="neet_level_medium")],
        [InlineKeyboardButton("🔴 Hard", callback_data="neet_level_hard"),
         InlineKeyboardButton("🎯 Mixed", callback_data="neet_level_mixed")],
        [InlineKeyboardButton("❌ Cancel", callback_data="neet_cancel")]
    ]
    return InlineKeyboardMarkup(kb)

def neet_keyboard_count():
    kb = [
        [InlineKeyboardButton("5", callback_data="neet_count_5"),
         InlineKeyboardButton("10", callback_data="neet_count_10"),
         InlineKeyboardButton("15", callback_data="neet_count_15")],
        [InlineKeyboardButton("20", callback_data="neet_count_20"),
         InlineKeyboardButton("25", callback_data="neet_count_25"),
         InlineKeyboardButton("30", callback_data="neet_count_30")],
        [InlineKeyboardButton("50 🔥", callback_data="neet_count_50"),
         InlineKeyboardButton("100 💪", callback_data="neet_count_100")],
        [InlineKeyboardButton("❌ Cancel", callback_data="neet_cancel")]
    ]
    return InlineKeyboardMarkup(kb)

def neet_keyboard_language():
    kb = [
        [InlineKeyboardButton("🇮🇳 Gujarati", callback_data="neet_lang_gujarati"),
         InlineKeyboardButton("🇮🇳 Hindi", callback_data="neet_lang_hindi"),
         InlineKeyboardButton("🇬🇧 English", callback_data="neet_lang_english")],
        [InlineKeyboardButton("❌ Cancel", callback_data="neet_cancel")]
    ]
    return InlineKeyboardMarkup(kb)

async def start_neet_flow(query, ctx, uid):
    neet_sessions[uid] = {"step": "level", "chat_id": query.message.chat_id}
    await query.edit_message_text(
        "🧬 <b>NEET Setup</b>\n\n<b>Step 1/3 — Difficulty Level choose karo:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=neet_keyboard_level()
    )

# ─── NEET Callback Handler ─────────────────────────────────────────────────
async def neet_callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    await query.answer()

    if not is_authorized(uid):
        await query.answer("Unauthorized!", show_alert=True)
        return

    data = query.data

    if data == "neet_cancel":
        neet_sessions.pop(uid, None)
        await query.edit_message_text("❌ Cancelled.")
        # Show main menu again
        name = query.from_user.first_name or "User"
        await show_main_menu_from_query(query, ctx, uid, name)
        return

    if data == "neet_start":
        await start_neet_flow(query, ctx, uid)
        return

    # Level selection
    if data.startswith("neet_level_"):
        level = data.replace("neet_level_", "")
        if uid not in neet_sessions:
            neet_sessions[uid] = {"chat_id": query.message.chat_id}
        neet_sessions[uid]["level"] = level
        neet_sessions[uid]["step"] = "count"
        names = {"easy": "🟢 Easy", "medium": "🟡 Medium", "hard": "🔴 Hard", "mixed": "🎯 Mixed"}
        await query.edit_message_text(
            f"✅ Level: <b>{names.get(level, level)}</b>\n\n<b>Step 2/3 — Kitne MCQ chahiye?</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=neet_keyboard_count()
        )
        return

    # Count selection
    if data.startswith("neet_count_"):
        count = int(data.replace("neet_count_", ""))
        if uid not in neet_sessions:
            neet_sessions[uid] = {"chat_id": query.message.chat_id}
        neet_sessions[uid]["count"] = count
        neet_sessions[uid]["step"] = "language"
        await query.edit_message_text(
            f"✅ MCQ Count: <b>{count}</b>\n\n<b>Step 3/3 — Language choose karo:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=neet_keyboard_language()
        )
        return

    # Language selection
    if data.startswith("neet_lang_"):
        lang = data.replace("neet_lang_", "")
        if uid not in neet_sessions:
            neet_sessions[uid] = {"chat_id": query.message.chat_id}
        neet_sessions[uid]["language"] = lang
        neet_sessions[uid]["step"] = "ready"
        neet_sessions[uid]["ready"] = True
        names = {"gujarati": "🇮🇳 Gujarati", "hindi": "🇮🇳 Hindi", "english": "🇬🇧 English"}
        await query.edit_message_text(
            f"✅ Language: <b>{names.get(lang, lang)}</b>\n\n"
            f"🎯 <b>Setup Complete!</b>\n\n"
            f"Ab <b>topic(s)</b> type karo (comma separated):\n"
            f"<i>Example: Cell Biology, Genetics, Human Digestive System</i>\n\n"
            f"Har baar topics bhejo, HTML quiz milega.\n"
            f"Settings change karne ke liye /neet use karo.",
            parse_mode=ParseMode.HTML
        )

# ─── /neet Command ─────────────────────────────────────────────────────────
async def cmd_neet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        await update.message.reply_text("🔒 Access nahi hai.")
        return
    chat_id = update.effective_chat.id
    # Start flow directly
    neet_sessions[uid] = {"step": "level", "chat_id": chat_id}
    await update.message.reply_text(
        "🧬 <b>NEET Setup</b>\n\n<b>Step 1/3 — Difficulty Level choose karo:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=neet_keyboard_level()
    )

# ─── Modified Text Handler (add NEET readiness) ──────────────────────────
# We'll extend the existing handle_text function.
# The original handle_text is defined above; we'll override it by redefining.
# We need to keep the existing logic for download, quiz_html, quiz_poll.
# We'll add a check for NEET ready state at the beginning.

# We'll replace the original handle_text definition with an augmented version.
# Since the file is long, we'll redefine it after the original one.
# But to avoid duplication, we'll just modify the existing handle_text function in place.
# We'll assume the code below replaces the original handle_text.

# However, because we are writing a single file, we can't have two definitions.
# We'll keep the original handle_text as is, but we need to insert the NEET check.
# Let's just copy the original handle_text and add the NEET check at the top.

# We'll define a new function and then override the handler registration later.
# But the original handle_text is already registered. We'll replace it by reassigning.

# Actually, we can just add a new MessageHandler for text that catches NEET topics first.
# But order matters: if we add a handler before the existing one, it will catch.
# We can add a new handler with filters that only trigger when user is in NEET ready state.
# That's safer: we add a new handler for text that checks if neet_sessions[uid].get("ready") is True.
# If not, it returns None (so the next handler will process).
# We'll add this handler before the existing one in main().

# So we'll keep the original handle_text as is (unchanged) and add a new handler for NEET topics.

# Let's write the new handler.

async def neet_topic_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        return  # let other handlers handle
    prefs = neet_sessions.get(uid)
    if not prefs or not prefs.get("ready"):
        return  # not in NEET mode, let other handlers
    text = update.message.text.strip()
    if text.startswith("/"):
        return

    # Process topics
    topics = [t.strip() for t in text.split(",") if t.strip()]
    if not topics:
        await update.message.reply_text("❌ Koi topic nahi mila. Comma separated topics bhejo.")
        return

    main_topic = topics[0]
    topic_str = ", ".join(topics)

    level = prefs.get("level", "medium")
    count = prefs.get("count", 10)
    language = prefs.get("language", "english")

    wait_msg = await update.message.reply_text(
        f"⏳ <b>Generating {count} questions...</b>\n\n"
        f"📚 Topics: <b>{topic_str}</b>\n"
        f"🎯 Level: <b>{level.title()}</b>\n"
        f"🌐 Language: <b>{language.title()}</b>\n\n"
        f"<i>Please wait...</i>",
        parse_mode=ParseMode.HTML
    )

    def progress_callback(done, total, batch, total_b):
        # We'll use asyncio.run_coroutine_threadsafe to update message
        # But we are in a thread, we need to schedule coroutine.
        # We'll use a queue or just update from the background task.
        # Since we'll run generation in a thread, we'll use a callback that sends updates via asyncio.
        # We'll define a coroutine to edit the message.
        pass

    # We'll launch a background task to generate and send the file.
    async def generate_and_send():
        try:
            # Run the synchronous generation in a thread
            def generate():
                return _neet_generate_all_mcqs(topic_str, level, count, language, progress_callback=None)
            questions = await asyncio.to_thread(generate)

            if not questions:
                await wait_msg.edit_text(
                    "❌ Questions generate nahi ho sake. Dobara try karo.\n\n"
                    "💡 Tips:\n"
                    "• Specific topics likho (e.g., 'Cell Division')\n"
                    "• Gujarati mein 5-10 questions try karo\n"
                    "• Baad mein dobara try karo"
                )
                return

            html_content = _neet_generate_html_quiz(questions, main_topic, level, language, len(questions))
            html_bytes = io.BytesIO(html_content.encode('utf-8'))
            html_bytes.seek(0)
            filename = f"NEET_Quiz_{main_topic[:20]}_{int(time.time())}.html"

            # Delete wait message and send document
            await wait_msg.delete()
            await ctx.bot.send_document(
                chat_id=update.effective_chat.id,
                document=("NEET_Quiz.html", html_bytes),
                caption=f"✅ <b>{len(questions)} questions</b> ready!\n\n"
                        f"📚 Topics: {topic_str}\n"
                        f"🎯 Level: {level.title()}\n"
                        f"🌐 Language: {language.title()}\n\n"
                        f"⬇️ HTML file download karo aur browser mein open karo.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"NEET generation error: {e}")
            try:
                await wait_msg.edit_text(f"❌ Error: {str(e)[:300]}")
            except:
                pass

    asyncio.create_task(generate_and_send())

# Now we need to register this handler in main() before the existing text handler.
# We'll also need to add the /neet command.

# ══════════════════════════════════════════════════════════════════════════════
#  /done (unchanged)
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    if not is_authorized(uid): return
    mode = user_mode.get(uid)

    if mode not in ("quiz_html","quiz_poll") or uid not in mcq_buffer:
        await update.message.reply_text("⚠️ Pehle Quiz mode choose karo aur MCQ paste karo."); return

    buf   = mcq_buffer.get(uid, {})
    texts = buf.get("chunks", [])
    title = buf.get("title", "Quiz")

    if not texts:
        await update.message.reply_text("⚠️ Buffer khali hai! Pehle MCQ paste karo."); return

    try: await update.message.delete()
    except: pass

    to_del = msg_to_delete.pop(uid, [])
    if to_del:
        asyncio.create_task(delete_messages(ctx.bot, update.effective_chat.id, to_del))

    combined = "\n".join(texts)
    mcq_buffer.pop(uid, None)

    chat_id = update.effective_chat.id
    if mode == "quiz_html":
        await handle_quiz_html(chat_id, ctx, combined, title)
    elif mode == "quiz_poll":
        await handle_quiz_poll(chat_id, ctx, combined)

# ─── DOCUMENT HANDLER (unchanged) ─────────────────────────────────────────
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid): return
    doc = update.message.document
    if not doc: return
    if not (doc.file_name.endswith(".txt") or "text" in (doc.mime_type or "")):
        await update.message.reply_text("⚠️ Sirf .txt file bhejo."); return

    msg = await update.message.reply_text("⏳ File read ho rahi hai...")
    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        content = buf.getvalue().decode("utf-8", errors="ignore")
    except Exception as e:
        await msg.edit_text(f"❌ File read error: {e}"); return

    mode = user_mode.get(uid)

    if mode == "quiz_html":
        sess = user_sessions.get(uid, {})
        title = mcq_buffer.get(uid, {}).get("title") or sess.get("title", "Quiz")
        questions = parse_questions(content)
        total_parsed = content.upper().count("ANS:")
        skipped = total_parsed - len(questions)
        if not questions:
            await msg.edit_text(
                "❌ *Koi valid MCQ nahi mila!*\n\n"
                "Check karo ki questions ka format theek ho:\n"
                "`Q1. Question\n(1) Option A\n(2) Option B\nANS: 1`",
                parse_mode=ParseMode.MARKDOWN); return
        skip_text = f"\n⚠️ _{skipped} questions skip hue (blank options/format issue)_" if skipped > 0 else ""
        await msg.edit_text(
            f"✅ *{len(questions)} questions ready!*{skip_text}\n⚙️ HTML ban rahi hai...",
            parse_mode=ParseMode.MARKDOWN)
        await handle_quiz_html(update.effective_chat.id, ctx, content, title)
        try: await msg.delete()
        except: pass
        return

    if mode == "quiz_poll":
        questions = parse_questions(content)
        total_parsed = content.upper().count("ANS:")
        skipped = total_parsed - len(questions)
        if not questions:
            await msg.edit_text("❌ Koi valid MCQ nahi mila!"); return
        skip_text = f"\n⚠️ _{skipped} questions skip hue_" if skipped > 0 else ""
        await msg.edit_text(
            f"✅ *{len(questions)} questions ready!*{skip_text}\n⏳ Polls ban rahe hain...",
            parse_mode=ParseMode.MARKDOWN)
        await handle_quiz_poll(update.effective_chat.id, ctx, content)
        try: await msg.delete()
        except: pass
        return

    # Default: PDF download
    user_mode[uid] = "download"
    entries = parse_links_file(content)
    if not entries:
        await msg.edit_text("❌ Koi valid link nahi!\n\n*Format:*\n`Name:https://url.pdf`", parse_mode=ParseMode.MARKDOWN); return
    user_sessions[uid] = {"entries":entries,"current_index":0,"waiting_start":True}
    await msg.edit_text(
        f"✅ *{len(entries)} PDF links mili!*\n\n📌 Konse number se start karna hai?\n_(1 se {len(entries)} tak)_",
        parse_mode=ParseMode.MARKDOWN)

# ─── TEXT HANDLER (original, unchanged) ──────────────────────────────────
# We'll keep the original handle_text as is; it's already defined earlier.
# We'll rename it to avoid conflict? But we already have it defined before.
# In the final file, we'll have the original handle_text from the first half.
# We'll just keep it and add the new neet_topic_handler separately.
# So we don't need to redefine handle_text.

# ─── QUIZ POLL ─────────────────────────────────────────────────────────────
async def handle_quiz_poll(chat_id, ctx, text):
    questions = parse_questions(text)
    if not questions:
        await safe_send(ctx.bot, chat_id, "❌ MCQ parse nahi hua!"); return
    msg = await safe_send(ctx.bot, chat_id, f"✅ *{len(questions)} questions!*\n⏳ Polls ban rahe hain...")
    success = fail = 0
    for idx,(q,opts,ans) in enumerate(questions):
        try:
            await ctx.bot.send_poll(chat_id=chat_id, question=q[:300],
                options=[o[:100] for o in opts[:10]], type=Poll.QUIZ, correct_option_id=ans, is_anonymous=False)
            success += 1
            if (idx+1)%5==0: await safe_edit(msg, f"⏳ *{idx+1}/{len(questions)} polls sent...*")
            await asyncio.sleep(0.6)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after+1)
            try:
                await ctx.bot.send_poll(chat_id=chat_id, question=q[:300],
                    options=[o[:100] for o in opts[:10]], type=Poll.QUIZ, correct_option_id=ans, is_anonymous=False)
                success += 1
            except: fail += 1
        except Exception as e:
            logger.error(f"Poll Q{idx+1}: {e}"); fail += 1
    result = f"✅ *{success} Quiz Polls sent!*"
    if fail: result += f"\n⚠️ {fail} fail"
    await safe_edit(msg, result)

# ─── QUIZ HTML (from pasted text) ─────────────────────────────────────────
async def handle_quiz_html(chat_id, ctx, text, title):
    questions = parse_questions(text)
    if not questions:
        await safe_send(ctx.bot, chat_id, "❌ MCQ parse nahi hua!"); return
    msg = await safe_send(ctx.bot, chat_id, f"⚙️ *{len(questions)} questions* se HTML ban rahi hai...")
    try:
        html_content = build_quiz_html(title, questions)
        safe_name = re.sub(r'[^\w\s\-.]','', title).strip().replace(' ','-')[:80]
        if not safe_name: safe_name = "quiz"
        filename = f"{safe_name}.html"
        filepath = DOWNLOAD_DIR / filename
        async with aiofiles.open(filepath,"w",encoding="utf-8") as f:
            await f.write(html_content)
        caption = (f"🌐 *{title}*\n\n"
                   f"📊 {len(questions)} Questions\n"
                   f"💾 {fmt_size(filepath.stat().st_size)}\n\n"
                   f"_Browser mein kholo — Dark theme quiz!_ 🔥\n\n"
                   f"Made by {CREDIT_TAG}")
        with open(filepath,"rb") as f:
            await ctx.bot.send_document(chat_id=chat_id, document=f,
                filename=filename, caption=caption, parse_mode=ParseMode.MARKDOWN)
        filepath.unlink(missing_ok=True)
        try: await msg.delete()
        except: pass
    except Exception as e:
        logger.error(f"HTML gen: {e}", exc_info=True)
        await safe_edit(msg, f"❌ HTML error: `{str(e)[:150]}`")

# ─── STOP ─────────────────────────────────────────────────────────────────
async def stop_cmd(update, ctx):
    uid = update.effective_user.id
    if not is_authorized(uid): return
    if uid in active_tasks:
        active_tasks[uid].cancel()
        await update.message.reply_text("⏹️ *Download stop.*\nResume ke liye /start karo.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("⚠️ Koi active download nahi.")

# ─── DOWNLOAD LOOP (unchanged) ────────────────────────────────────────────
async def run_downloads(uid, ctx, chat_id, start_idx):
    sess = user_sessions.get(uid,{}); entries = sess.get("entries",[]); total = len(entries)
    progress_msg = await safe_send(ctx.bot, chat_id, "📊 *Download shuru ho raha hai...*")
    last_edit = [0.0]
    for i in range(start_idx, total):
        sess["current_index"] = i
        rs = load_resume(); rs[str(uid)] = sess; save_resume(rs)
        entry = entries[i]; name = entry["name"]; url = entry["url"]
        safe_name = name.replace("/","-").replace("\\","-").replace(":","-")[:100]
        filename = f"{safe_name}.pdf"; filepath = DOWNLOAD_DIR/filename
        if filepath.exists(): filepath.unlink()
        async def update_progress(downloaded,file_total,speed,_i=i,_name=name):
            now=time.time()
            if now-last_edit[0]<3: return
            last_edit[0]=now
            bar=progress_bar(downloaded,file_total)
            pct=f"{downloaded*100//file_total}%" if file_total>0 else "..."
            txt=(f"📥 *Downloading {_i+1}/{total}*\n📄 `{_name[:45]}`\n\n"
                 f"`{bar}` {pct}\n💾 {fmt_size(downloaded)}"
                 +(f" / {fmt_size(file_total)}" if file_total else "")
                 +f"\n⚡ {fmt_speed(speed)}\n\n✅ Done: {_i-start_idx} | ⏳ Left: {total-_i-1}")
            await safe_edit(progress_msg,txt)
        try:
            if progress_msg: await safe_edit(progress_msg,f"📥 *Downloading {i+1}/{total}*\n`{name[:50]}`\n⏳ Please wait...")
            ok = await download_file(url,filepath,update_progress,retries=4)
            if not ok: await safe_send(ctx.bot,chat_id,f"⚠️ *Skip #{i+1}*\n`{name[:50]}`"); continue
            file_size=filepath.stat().st_size
            if progress_msg: await safe_edit(progress_msg,f"📤 *Uploading {i+1}/{total}*\n`{name[:50]}`\n💾 {fmt_size(file_size)}")
            uploaded=await safe_send_doc(ctx.bot,chat_id,filepath,filename,f"📄 *{name}*\n\n📥 Download by {CREDIT_TAG}",retries=5)
            if filepath.exists(): filepath.unlink()
            if not uploaded: await safe_send(ctx.bot,chat_id,f"⚠️ *Upload fail #{i+1}*\n`{name[:50]}`"); continue
            if progress_msg: await safe_edit(progress_msg,f"✅ *{i+1}/{total} done!*\n📄 `{name[:50]}`\n\n⏳ Next file...")
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            await safe_send(ctx.bot,chat_id,f"⏹️ *Stopped at #{i+1}*\nResume ke liye /start karo.")
            if filepath.exists(): filepath.unlink(); return
        except Exception as e:
            logger.error(f"Error #{i+1}: {e}",exc_info=True)
            await safe_send(ctx.bot,chat_id,f"⚠️ *Error #{i+1}* (skipping)\n`{str(e)[:100]}`")
            if filepath.exists(): filepath.unlink()
            await asyncio.sleep(3); continue
    rs=load_resume(); rs.pop(str(uid),None); save_resume(rs)
    active_tasks.pop(uid,None); user_sessions.pop(uid,None)
    await safe_send(ctx.bot,chat_id,f"🎉 *Sab files complete!*\n\n✅ Total: {total-start_idx} files\n📥 Credit: `{CREDIT_TAG}`")

# ─── HEALTH ────────────────────────────────────────────────────────────────
async def health_handler(request): return web.Response(text="OK",status=200)

# ─── MAIN ──────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN: raise ValueError("BOT_TOKEN not set!")
    tg_app = Application.builder().token(BOT_TOKEN).build()

    # Existing handlers
    tg_app.add_handler(CommandHandler("start",      start))
    tg_app.add_handler(CommandHandler("stop",       stop_cmd))
    tg_app.add_handler(CommandHandler("done",       cmd_done))
    tg_app.add_handler(CommandHandler("adduser",    cmd_adduser))
    tg_app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    tg_app.add_handler(CommandHandler("users",      cmd_users))
    tg_app.add_handler(CommandHandler("myid",       cmd_myid))

    # NEW: NEET commands
    tg_app.add_handler(CommandHandler("neet",       cmd_neet))

    # Callback handlers
    # First, NEET-specific callbacks
    tg_app.add_handler(CallbackQueryHandler(neet_callback_handler, pattern="^neet_"))
    # Then the generic button handler (for non-NEET)
    tg_app.add_handler(CallbackQueryHandler(button_handler))

    # Document handler (unchanged)
    tg_app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Text handlers: first check NEET topics, then fallback to existing
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, neet_topic_handler))
    # The original text handler (handle_text) is still defined above and will be added
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Health server
    http_app = web.Application()
    http_app.router.add_get("/", health_handler)
    http_app.router.add_get("/health", health_handler)
    port = int(os.environ.get("PORT",8000))

    async def run_all():
        runner = web.AppRunner(http_app)
        await runner.setup()
        await web.TCPSite(runner,"0.0.0.0",port).start()
        logger.info(f"Health server :{port}")
        async with tg_app:
            await tg_app.start()
            logger.info("Bot started!")
            await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=True)
            while True: await asyncio.sleep(3600)

    asyncio.run(run_all())

if __name__ == "__main__":
    main()