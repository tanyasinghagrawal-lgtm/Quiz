import asyncio
import base64
import logging
import os
from datetime import datetime, timedelta
import json
import aiohttp

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, 
    ChatMember, ChatMemberUpdated
)
from telegram.constants import ParseMode, ChatMemberStatus, ChatType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ChatMemberHandler, ContextTypes
)

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, Mapped, mapped_column
from sqlalchemy import select, String, BigInteger, JSON, DateTime

# ================= CONFIGURATION ================= #
BOT_TOKEN = "8997704779:AAH5nvuXyx3jW90qfSHolYV2HtTWQs1Grog"  # Replace with actual Bot Token
MAIN_ADMIN_ID = 6796088344
WEBHOOK_DOMAIN = "https://expert-octo-adventure-yhfh.onrender.com"  # Your Render URL
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_DOMAIN}{WEBHOOK_PATH}"

# For Render, always bind to 0.0.0.0 and use the PORT environment variable
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.environ.get("PORT", 10000)) 

DATABASE_URL = "postgresql+asyncpg://avnadmin:AVNS_jTfrFSn4cMYbutIKDKN@pg-88cc622-youtrendsfunny-a945.a.aivencloud.com:22179/defaultdb?ssl=require"
_ENCODED_GEMINI_KEY = "QVEuQWI4Uk42TFJWVmZSNTAxV1dodXEwZUZESzh2NVlqOVZUa1hyMnlLa1ozeHJlT0RtQWc="
GEMINI_API_KEY = base64.b64decode(_ENCODED_GEMINI_KEY).decode('utf-8')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= DATABASE SETUP ================= #
engine = create_async_engine(DATABASE_URL, echo=False, pool_size=20, max_overflow=30)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

class AllowedGroup(Base):
    __tablename__ = 'tgquizbot_allowed_groups'
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=True)

class Quiz(Base):
    __tablename__ = 'tgquizbot_quizzes'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    start_time: Mapped[datetime] = mapped_column(DateTime)
    reward_msg: Mapped[str] = mapped_column(String, nullable=True)
    notif_msg: Mapped[str] = mapped_column(String, nullable=True)
    time_per_q: Mapped[int] = mapped_column(BigInteger)
    solution_msg: Mapped[str] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="scheduled") 

class Question(Base):
    __tablename__ = 'tgquizbot_questions'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    quiz_id: Mapped[int] = mapped_column(BigInteger)
    q_text: Mapped[str] = mapped_column(String)
    options: Mapped[dict] = mapped_column(JSON) 
    correct_idx: Mapped[int] = mapped_column(BigInteger)


# Caches for high performance
active_votes_cache = {} 

# Conversation States
(
    START_TIME, REWARD_MSG, TIME_PER_Q, Q_TEXT, 
    OPT1, OPT2, OPT3, OPT4, CORRECT_OPT, 
    NEXT_ACTION, NOTIF_MSG, SOL_TYPE, MANUAL_SOL
) = range(13)

# ================= HELPER FUNCTIONS ================= #
def parse_custom_date(date_string):
    formats = [
        "%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M", 
        "%d-%m-%Y %I:%M %p", "%d-%m-%Y %H:%M"
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_string.strip(), fmt)
        except ValueError:
            pass
    return None

async def generate_gemini_solution(prompt: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3}
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    return data['candidates'][0]['content']['parts'][0]['text']
                else:
                    err = await response.text()
                    logger.error(f"Gemini API Error: {err}")
                    return "<i>Failed to generate AI solution. Please check API limits.</i>"
        except Exception as e:
            logger.error(f"HTTP Error to Gemini: {e}")
            return "<i>Failed to connect to AI server.</i>"

# ================= GROUP ADD/REMOVE LOGIC ================= #
async def track_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Extracts status change and notifies admin if added to a group."""
    result = update.my_chat_member
    if not result:
        return

    was_member = result.old_chat_member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR]
    is_member = result.new_chat_member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR]

    if not was_member and is_member and result.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        chat = result.chat
        keyboard = [
            [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{chat.id}"),
             InlineKeyboardButton("❌ Deny", callback_data=f"deny_{chat.id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = (
            f"🔔 <b>New Group Request</b>\n\n"
            f"<b>Group Name:</b> <code>{chat.title}</code>\n"
            f"<b>Group ID:</b> <code>{chat.id}</code>\n\n"
            f"<i>Would you like to allow quizzes in this group?</i>"
        )
        try:
            await context.bot.send_message(MAIN_ADMIN_ID, text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Failed to message main admin: {e}")

async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != MAIN_ADMIN_ID:
        await query.answer("Unauthorized", show_alert=True)
        return
        
    action, group_id_str = query.data.split("_")
    group_id = int(group_id_str)
    
    if action == "approve":
        async with AsyncSessionLocal() as session:
            session.add(AllowedGroup(group_id=group_id, title="Approved"))
            await session.commit()
        
        try:
            await context.bot.send_message(group_id, "✅ <b>Group Approved!</b>\nAdmins can now use /create to setup quizzes.", parse_mode=ParseMode.HTML)
        except Exception:
            pass # Group might have kicked bot
            
        await query.edit_message_text(f"✅ <b>Group {group_id} has been approved.</b>", parse_mode=ParseMode.HTML)
    else:
        try:
            await context.bot.send_message(group_id, "❌ <b>Request Denied.</b>\nThis bot is not authorized to operate here. Leaving...", parse_mode=ParseMode.HTML)
            await context.bot.leave_chat(group_id)
        except Exception:
            pass
        await query.edit_message_text(f"❌ <b>Group {group_id} denied and bot left.</b>", parse_mode=ParseMode.HTML)

# ================= CREATE COMMAND (IN GROUP) ================= #
async def cmd_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
        
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(AllowedGroup).where(AllowedGroup.group_id == chat_id))
        if not result.scalar():
            await update.message.reply_text("⚠️ <i>This group is not approved for quizzes yet. Please wait for main admin approval.</i>", parse_mode=ParseMode.HTML)
            return

    member = await context.bot.get_chat_member(chat_id, user_id)
    if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
        await update.message.reply_text("⛔️ <b>Access Denied:</b> Only group administrators can create quizzes.", parse_mode=ParseMode.HTML)
        return

    bot_info = await context.bot.get_me()
    deep_link = f"https://t.me/{bot_info.username}?start=grp_{chat_id}"
    
    keyboard = [[InlineKeyboardButton("🛠 Create Quiz", url=deep_link)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("👨‍💻 <b>Admin recognized!</b>\nClick the button below to create your quiz securely in my DMs.", reply_markup=reply_markup, parse_mode=ParseMode.HTML)

# ================= QUIZ CREATION (CONVERSATION HANDLER) ================= #
async def start_creation_dm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles deep link in DM to start quiz creation."""
    if update.effective_chat.type != ChatType.PRIVATE:
        return ConversationHandler.END

    args = context.args
    if not args or not args[0].startswith("grp_"):
        await update.message.reply_text("👋 <b>Welcome to the Ultimate Quiz Bot!</b>\n\nAdd me to a group to host highly customizable and anti-cheat protected quizzes. \n<i>(Main admin approval required)</i>", parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    try:
        group_id = int(args[0].replace("grp_", ""))
        member = await context.bot.get_chat_member(group_id, update.effective_user.id)
        if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            await update.message.reply_text("⛔️ You are not an administrator in that group.", parse_mode=ParseMode.HTML)
            return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text("⚠️ Invalid group ID or bot lacks permissions in the group.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    context.user_data.clear() # Reset any past states
    context.user_data['group_id'] = group_id
    context.user_data['questions'] = []
    
    text = (
        "📅 <b>Step 1: Schedule the Quiz</b>\n\n"
        "Please provide the exact start date and time.\n"
        "<b>Format:</b> <code>DD/MM/YYYY HH:MM pm/am</code> or <code>DD/MM/YYYY HH:MM</code> (24hr)\n"
        "<i>Example:</i> <code>27/02/2032 8:30 pm</code>\n\n"
        "Type /cancel at any time to abort."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    return START_TIME

async def process_start_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    dt = parse_custom_date(update.message.text)
    if not dt:
        await update.message.reply_text("❌ <b>Invalid format!</b>\nPlease try again (e.g., <code>27/02/2032 8:30 pm</code>).", parse_mode=ParseMode.HTML)
        return START_TIME
        
    if dt < datetime.now():
        await update.message.reply_text("⏳ <b>Time Travel not supported!</b>\nThe time must be in the future. Please try again.", parse_mode=ParseMode.HTML)
        return START_TIME
        
    context.user_data['start_time'] = dt.isoformat()
    await update.message.reply_text("🎁 <b>Step 2: Reward Message</b>\n\nSend a text message for the winners (e.g., how to claim rewards).\nOr simply type <code>skip</code> if there are no rewards.", parse_mode=ParseMode.HTML)
    return REWARD_MSG

async def process_reward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text_content = update.message.text_html
    msg = text_content if update.message.text.lower() != 'skip' else None
    context.user_data['reward_msg'] = msg
    
    keyboard = [
        [InlineKeyboardButton(f"⏱ {i} sec", callback_data=f"time_{i}") for i in [10, 20, 30]],
        [InlineKeyboardButton(f"⏱ {i} sec", callback_data=f"time_{i}") for i in [40, 50, 60]]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("⏱ <b>Step 3: Time Per Question</b>\n\nSelect how long users have to answer each question:", reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    return TIME_PER_Q

async def process_time_q(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    t = int(query.data.split("_")[1])
    context.user_data['time_per_q'] = t
    
    await query.edit_message_text(f"✅ Selected: <b>{t} seconds</b> per question.\n\n📝 <b>Step 4: Create Questions</b>\n\nPlease send the text for your <b>FIRST QUESTION</b>:", parse_mode=ParseMode.HTML)
    return Q_TEXT

async def process_q_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['current_q'] = update.message.text_html
    await update.message.reply_text("🅰️ Send <b>Option 1</b>:", parse_mode=ParseMode.HTML)
    return OPT1

async def p_opt1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    context.user_data['opt1'] = update.message.text
    await update.message.reply_text("🅱️ Send <b>Option 2</b>:", parse_mode=ParseMode.HTML)
    return OPT2

async def p_opt2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    context.user_data['opt2'] = update.message.text
    await update.message.reply_text("©️ Send <b>Option 3</b>:", parse_mode=ParseMode.HTML)
    return OPT3

async def p_opt3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    context.user_data['opt3'] = update.message.text
    await update.message.reply_text("🅳 Send <b>Option 4</b>:", parse_mode=ParseMode.HTML)
    return OPT4

async def p_opt4(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['opt4'] = update.message.text
    keyboard = [
        [InlineKeyboardButton("1️⃣", callback_data="corr_1"), InlineKeyboardButton("2️⃣", callback_data="corr_2")],
        [InlineKeyboardButton("3️⃣", callback_data="corr_3"), InlineKeyboardButton("4️⃣", callback_data="corr_4")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🎯 <b>Which option is correct?</b>\nSelect the right answer below:", reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    return CORRECT_OPT

async def process_correct_opt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    corr = int(query.data.split("_")[1])
    
    q_dict = {
        'q_text': context.user_data['current_q'],
        'options': [context.user_data['opt1'], context.user_data['opt2'], context.user_data['opt3'], context.user_data['opt4']],
        'correct_idx': corr - 1
    }
    context.user_data['questions'].append(q_dict)
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Another Question", callback_data="next_q")],
        [InlineKeyboardButton("🛑 Stop & Finalize", callback_data="stop_q")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(f"✅ <b>Question saved!</b>\nTotal questions added: <b>{len(context.user_data['questions'])}</b>", reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    return NEXT_ACTION

async def process_next_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "next_q":
        await query.edit_message_text("📝 Please send the text for your <b>NEXT QUESTION</b>:", parse_mode=ParseMode.HTML)
        return Q_TEXT
    else:
        text = (
            "📢 <b>Step 5: Notification Message</b>\n\n"
            "Send the message to broadcast to the group during reminders.\n"
            "<i>Example: 'Join the grand quiz! The 1st and 2nd winners will get special rewards!'</i>"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
        return NOTIF_MSG

async def process_notif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['notif_msg'] = update.message.text_html
    keyboard = [
        [InlineKeyboardButton("🤖 Generate with AI (Gemini)", callback_data="sol_ai")],
        [InlineKeyboardButton("✍️ Set Manually", callback_data="sol_man")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("💡 <b>Step 6: Solutions</b>\n\nHow would you like to provide the solutions for this quiz?", reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    return SOL_TYPE

async def process_sol_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data == "sol_man":
        await query.edit_message_text("✍️ <b>Manual Solutions</b>\n\nPlease send all explanations in <b>ONE single message</b>.", parse_mode=ParseMode.HTML)
        return MANUAL_SOL
    else:
        await query.edit_message_text("🤖 <i>Generating beautifully detailed AI solutions... Please wait.</i>", parse_mode=ParseMode.HTML)
        
        prompt = "Provide a short but highly detailed and accurate solution for the following quiz questions. Use clear HTML formatting (<b>, <i>, <code>). Do not use Markdown.\n\n"
        for i, q in enumerate(context.user_data['questions']):
            prompt += f"Question {i+1}: {q['q_text']}\nOptions: {', '.join(q['options'])}\nCorrect Answer: {q['options'][q['correct_idx']]}\n\n"
        
        sol_text = await generate_gemini_solution(prompt)
        
        await context.bot.send_message(query.from_user.id, f"✅ <b>AI Solutions Generated:</b>\n\n{sol_text}", parse_mode=ParseMode.HTML)
        await finalize_and_save_quiz(context, sol_text, query.from_user.id, update, is_callback=True)
        return ConversationHandler.END

async def process_manual_sol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await finalize_and_save_quiz(context, update.message.text_html, update.message.from_user.id, update, is_callback=False)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    await update.message.reply_text("❌ Quiz creation cancelled.", parse_mode=ParseMode.HTML)
    return ConversationHandler.END

async def finalize_and_save_quiz(context: ContextTypes.DEFAULT_TYPE, solution: str, admin_id: int, update: Update, is_callback: bool):
    data = context.user_data
    start_time = datetime.fromisoformat(data['start_time'])
    
    async with AsyncSessionLocal() as session:
        quiz = Quiz(
            group_id=data['group_id'],
            admin_id=admin_id,
            start_time=start_time,
            reward_msg=data['reward_msg'],
            notif_msg=data['notif_msg'],
            time_per_q=data['time_per_q'],
            solution_msg=solution,
            status="scheduled"
        )
        session.add(quiz)
        await session.flush()
        
        for q in data['questions']:
            session.add(Question(
                quiz_id=quiz.id, q_text=q['q_text'], options=q['options'], correct_idx=q['correct_idx']
            ))
        await session.commit()
        
        # Add to Job Queue immediately
        schedule_quiz_jobs(context.job_queue, quiz)
        
    success_text = (
        "🎉 <b>Quiz Successfully Scheduled!</b>\n\n"
        f"📅 <b>Start Time:</b> {start_time.strftime('%d/%m/%Y %I:%M %p')}\n"
        f"⏱ <b>Per Question:</b> {data['time_per_q']}s\n"
        f"📊 <b>Total Questions:</b> {len(data['questions'])}\n\n"
        "<i>Automated reminders have been set and the quiz will run flawlessly!</i>"
    )
    
    if is_callback:
        await update.callback_query.message.reply_text(success_text, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(success_text, parse_mode=ParseMode.HTML)

# ================= BACKGROUND JOBS ================= #
def schedule_quiz_jobs(job_queue, quiz: Quiz):
    """Schedules the reminders and the exact start of a quiz."""
    now = datetime.now()
    start_time = quiz.start_time
    
    if start_time < now:
        # Time passed while bot was offline, mark cancelled or run immediately?
        # Safe approach: Run it shortly if missed recently, otherwise skip.
        if (now - start_time).total_seconds() < 3600: 
            job_queue.run_once(run_quiz_job, when=10, data=quiz.id, name=f"quiz_start_{quiz.id}")
        return

    # Start Job
    job_queue.run_once(run_quiz_job, when=start_time, data=quiz.id, name=f"quiz_start_{quiz.id}")
    
    # Reminders
    reminders = [60, 15, 10, 5, 2, 1] # minutes
    for m in reminders:
        rem_time = start_time - timedelta(minutes=m)
        if rem_time > now:
            job_queue.run_once(
                quiz_reminder_job, 
                when=rem_time, 
                data={'group_id': quiz.group_id, 'mins': m, 'msg': quiz.notif_msg}, 
                name=f"quiz_rem_{quiz.id}_{m}"
            )

async def quiz_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    m = data['mins']
    if m > 1:
        text = f"📢 <b>Quiz Alert!</b>\n{data['msg']}\n\n⏳ <b>Starting in exactly {m} minutes!</b> Be ready!"
    else:
        text = f"🔥 <b>BE READY!</b>\n\nThe quiz is starting in <b>1 MINUTE!</b> 🚀"
        
    try:
        await context.bot.send_message(data['group_id'], text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Failed to send reminder to {data['group_id']}: {e}")

async def run_quiz_job(context: ContextTypes.DEFAULT_TYPE):
    quiz_id = context.job.data
    
    async with AsyncSessionLocal() as session:
        quiz = await session.get(Quiz, quiz_id)
        if not quiz or quiz.status != "scheduled": return
        
        quiz.status = "running"
        await session.commit()
        
        result = await session.execute(select(Question).where(Question.quiz_id == quiz_id))
        questions = result.scalars().all()

    group_id = quiz.group_id
    active_votes_cache[quiz_id] = {}
    scores = {} # user_id -> int correctly answered
    user_names = {} # user_id -> name (for leaderboard)

    try:
        await context.bot.send_message(group_id, "🚀 <b>THE QUIZ HAS STARTED!</b>\nRead carefully, you only get one vote per question!", parse_mode=ParseMode.HTML)
    except Exception:
        return # Cannot send to group

    for i, q in enumerate(questions):
        kb_buttons = []
        for idx, opt in enumerate(q.options):
            kb_buttons.append([InlineKeyboardButton(text=opt, callback_data=f"v_{quiz_id}_{q.id}_{idx}")])
        reply_markup = InlineKeyboardMarkup(kb_buttons)
        
        q_text = (
            f"❓ <b>Question {i+1}/{len(questions)}</b>\n\n"
            f"<b>{q.q_text}</b>\n\n"
            f"<i>⏱ Time limit: {quiz.time_per_q} seconds</i>"
        )
        
        try:
            msg = await context.bot.send_message(
                group_id, 
                q_text, 
                reply_markup=reply_markup, 
                parse_mode=ParseMode.HTML, 
                protect_content=True # CRITICAL ANTI-CHEAT
            )
            
            await asyncio.sleep(quiz.time_per_q)
            
            # Close voting silently
            try:
                await context.bot.edit_message_reply_markup(chat_id=group_id, message_id=msg.message_id, reply_markup=None)
            except Exception:
                pass 
                
            await asyncio.sleep(2) # Brief buffer before next question
            
            # Tally scores
            q_votes = active_votes_cache[quiz_id].get(q.id, {})
            for uid, user_data in q_votes.items():
                choice = user_data['choice']
                user_names[uid] = user_data['name']
                if choice == q.correct_idx:
                    scores[uid] = scores.get(uid, 0) + 1
        except Exception as e:
            logger.error(f"Error executing question {i+1} for quiz {quiz_id}: {e}")

    # Process Results
    async with AsyncSessionLocal() as session:
        quiz = await session.get(Quiz, quiz_id)
        quiz.status = "completed"
        await session.commit()

    if scores:
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        lb_text = "🏆 <b>FINAL LEADERBOARD</b> 🏆\n\n"
        medals = ["🥇", "🥈", "🥉"]
        for rank, (uid, score) in enumerate(sorted_scores[:10]):
            medal = medals[rank] if rank < 3 else f"{rank+1}."
            lb_text += f"{medal} <b>{user_names[uid]}</b> - {score}/{len(questions)} correct\n"
    else:
        lb_text = "😔 <b>Quiz Ended</b>\nNo one scored any points. Better luck next time!"

    await context.bot.send_message(group_id, lb_text, parse_mode=ParseMode.HTML)
    
    if quiz.reward_msg:
        await asyncio.sleep(1)
        await context.bot.send_message(group_id, f"🎁 <b>Rewards Info:</b>\n\n{quiz.reward_msg}", parse_mode=ParseMode.HTML)
        
    if quiz.solution_msg:
        await asyncio.sleep(2)
        await context.bot.send_message(group_id, f"💡 <b>Detailed Solutions:</b>\n\n{quiz.solution_msg}", parse_mode=ParseMode.HTML)

    if quiz_id in active_votes_cache:
        del active_votes_cache[quiz_id]

async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, qz_id_str, q_id_str, opt_idx_str = query.data.split("_")
    qz_id, q_id, opt_idx = int(qz_id_str), int(q_id_str), int(opt_idx_str)
    uid = query.from_user.id
    name = query.from_user.first_name
    
    if qz_id not in active_votes_cache:
        await query.answer("⏳ This quiz is no longer active!", show_alert=True)
        return
        
    if q_id not in active_votes_cache[qz_id]:
        active_votes_cache[qz_id][q_id] = {}
        
    if uid in active_votes_cache[qz_id][q_id]:
        await query.answer("⚠️ You have already cast your vote for this question!", show_alert=True)
        return
        
    # SILENT VOTE RECORDING
    active_votes_cache[qz_id][q_id][uid] = {'choice': opt_idx, 'name': name}
    await query.answer("✅ Vote recorded secretly! 🤫", show_alert=False)

async def load_pending_quizzes(application: Application):
    """Loads interrupted scheduled quizzes on bot startup."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Quiz).where(Quiz.status == 'scheduled'))
        quizzes = result.scalars().all()
        for quiz in quizzes:
            schedule_quiz_jobs(application.job_queue, quiz)
        logger.info(f"Loaded {len(quizzes)} scheduled quizzes into the job queue.")

def main():
    """Builds and starts the application."""
    # Ensure Tables Exist
    async def create_tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    
    # We must run table creation synchronously before starting the app
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(create_tables())

    application = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(ChatMemberHandler(track_chats, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CallbackQueryHandler(handle_approval, pattern="^(approve|deny)_"))
    application.add_handler(CommandHandler("create", cmd_create))
    application.add_handler(CallbackQueryHandler(handle_vote, pattern="^v_"))

    # Quiz Creation Conversation Handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_creation_dm)],
        states={
            START_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_start_time)],
            REWARD_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_reward)],
            TIME_PER_Q: [CallbackQueryHandler(process_time_q, pattern="^time_")],
            Q_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_q_text)],
            OPT1: [MessageHandler(filters.TEXT & ~filters.COMMAND, p_opt1)],
            OPT2: [MessageHandler(filters.TEXT & ~filters.COMMAND, p_opt2)],
            OPT3: [MessageHandler(filters.TEXT & ~filters.COMMAND, p_opt3)],
            OPT4: [MessageHandler(filters.TEXT & ~filters.COMMAND, p_opt4)],
            CORRECT_OPT: [CallbackQueryHandler(process_correct_opt, pattern="^corr_")],
            NEXT_ACTION: [CallbackQueryHandler(process_next_action, pattern="^(next_q|stop_q)$")],
            NOTIF_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_notif)],
            SOL_TYPE: [CallbackQueryHandler(process_sol_type, pattern="^sol_")],
            MANUAL_SOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_manual_sol)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    application.add_handler(conv_handler)

    # Pre-load quizzes into job queue
    loop.run_until_complete(load_pending_quizzes(application))

    # Run Webhook securely (This handles the 404 issue!)
    logger.info(f"Starting Webhook on {WEBAPP_HOST}:{WEBAPP_PORT} -> {WEBHOOK_URL}")
    application.run_webhook(
        listen=WEBAPP_HOST,
        port=WEBAPP_PORT,
        url_path=f"webhook/{BOT_TOKEN}",
        webhook_url=WEBHOOK_URL
    )

if __name__ == "__main__":
    main()
