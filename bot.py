import asyncio
import base64
import logging
from datetime import datetime, timedelta
import json

from aiogram import Bot, Dispatcher, F, types, Router
from aiogram.filters import Command, CommandStart, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
import aiohttp

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, Mapped, mapped_column
from sqlalchemy import select, String, BigInteger, JSON, DateTime

# ================= CONFIGURATION ================= #
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # Replace with your actual Bot Token
MAIN_ADMIN_ID = 6796088344
WEBHOOK_DOMAIN = "https://your-render-app-url.onrender.com"  # Replace with actual Render URL later
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_DOMAIN}{WEBHOOK_PATH}"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = 10000

DATABASE_URL = "postgresql+asyncpg://avnadmin:AVNS_jTfrFSn4cMYbutIKDKN@pg-88cc622-youtrendsfunny-a945.a.aivencloud.com:22179/defaultdb?ssl=require"
_ENCODED_GEMINI_KEY = "QVEuQWI4Uk42TFJWVmZSNTAxV1dodXEwZUZESzh2NVlqOVZUa1hyMnlLa1ozeHJlT0RtQWc="
GEMINI_API_KEY = base64.b64decode(_ENCODED_GEMINI_KEY).decode('utf-8')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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

# ================= INITIALIZATION ================= #
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# Caches for high performance
active_votes_cache = {} 
active_quiz_tasks = {}

# ================= STATES ================= #
class QuizForm(StatesGroup):
    group_id = State()
    start_time = State()
    reward_msg = State()
    time_per_q = State()
    q_text = State()
    opt1 = State()
    opt2 = State()
    opt3 = State()
    opt4 = State()
    correct_opt = State()
    next_action = State()
    notif_msg = State()
    solution_type = State()
    manual_solution = State()

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
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
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
                    logging.error(f"Gemini API Error: {err}")
                    return "<i>Failed to generate AI solution. Please check API limits.</i>"
        except Exception as e:
            logging.error(f"HTTP Error to Gemini: {e}")
            return "<i>Failed to connect to AI server.</i>"

# ================= GROUP ADD/REMOVE LOGIC ================= #
@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def on_bot_added(event: ChatMemberUpdated):
    if event.chat.type in ['group', 'supergroup']:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{event.chat.id}"),
             InlineKeyboardButton(text="❌ Deny", callback_data=f"deny_{event.chat.id}")]
        ])
        text = (
            f"🔔 <b>New Group Request</b>\n\n"
            f"<b>Group Name:</b> <code>{event.chat.title}</code>\n"
            f"<b>Group ID:</b> <code>{event.chat.id}</code>\n\n"
            f"<i>Would you like to allow quizzes in this group?</i>"
        )
        await bot.send_message(MAIN_ADMIN_ID, text, reply_markup=keyboard, parse_mode="HTML")

@router.callback_query(F.data.startswith("approve_") | F.data.startswith("deny_"))
async def handle_approval(callback: types.CallbackQuery):
    if callback.from_user.id != MAIN_ADMIN_ID:
        return
    
    action, group_id_str = callback.data.split("_")
    group_id = int(group_id_str)
    
    if action == "approve":
        async with AsyncSessionLocal() as session:
            session.add(AllowedGroup(group_id=group_id, title="Approved"))
            await session.commit()
        
        await bot.send_message(group_id, "✅ <b>Group Approved!</b>\nAdmins can now use /create to setup quizzes.", parse_mode="HTML")
        await callback.message.edit_text(f"✅ <b>Group {group_id} has been approved.</b>", parse_mode="HTML")
    else:
        await bot.send_message(group_id, "❌ <b>Request Denied.</b>\nThis bot is not authorized to operate here. Leaving...", parse_mode="HTML")
        await bot.leave_chat(group_id)
        await callback.message.edit_text(f"❌ <b>Group {group_id} denied and bot left.</b>", parse_mode="HTML")

# ================= CREATE COMMAND ================= #
@router.message(Command("start"))
async def cmd_start_general(message: types.Message, state: FSMContext):
    if message.chat.type == 'private' and not message.text.startswith("/start grp_"):
        await message.reply("👋 <b>Welcome to the Ultimate Quiz Bot!</b>\n\nAdd me to a group to host highly customizable and anti-cheat protected quizzes. \n<i>(Main admin approval required)</i>", parse_mode="HTML")

@router.message(Command("create"))
async def cmd_create(message: types.Message):
    if message.chat.type not in ['group', 'supergroup']:
        return
    
    async with AsyncSessionLocal() as session:
        is_allowed = await session.execute(select(AllowedGroup).where(AllowedGroup.group_id == message.chat.id))
        if not is_allowed.scalar():
            await message.reply("⚠️ <i>This group is not approved for quizzes yet. Please wait for main admin approval.</i>", parse_mode="HTML")
            return

    user_member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if user_member.status not in ['administrator', 'creator']:
        await message.reply("⛔️ <b>Access Denied:</b> Only group administrators can create quizzes.", parse_mode="HTML")
        return

    bot_info = await bot.get_me()
    deep_link = f"https://t.me/{bot_info.username}?start=grp_{message.chat.id}"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛠 Create Quiz", url=deep_link)]])
    await message.reply("👨‍💻 <b>Admin recognized!</b>\nClick the button below to create your quiz securely in my DMs.", reply_markup=kb, parse_mode="HTML")

@router.message(CommandStart(), F.text.startswith("/start grp_"))
async def start_creation_dm(message: types.Message, state: FSMContext):
    try:
        group_id = int(message.text.split("_")[1])
        user_member = await bot.get_chat_member(group_id, message.from_user.id)
        if user_member.status not in ['administrator', 'creator']:
            await message.reply("⛔️ You are not an administrator in that group.", parse_mode="HTML")
            return
    except Exception as e:
        await message.reply("⚠️ Invalid group ID or bot lacks permissions.", parse_mode="HTML")
        return

    await state.update_data(group_id=group_id, questions=[])
    text = (
        "📅 <b>Step 1: Schedule the Quiz</b>\n\n"
        "Please provide the exact start date and time.\n"
        "<b>Format:</b> <code>DD/MM/YYYY HH:MM pm/am</code> or <code>DD/MM/YYYY HH:MM</code> (24hr)\n"
        "<i>Example:</i> <code>27/02/2032 8:30 pm</code>"
    )
    await message.reply(text, parse_mode="HTML")
    await state.set_state(QuizForm.start_time)

# ================= FSM QUIZ CREATION ================= #
@router.message(QuizForm.start_time)
async def process_start_time(message: types.Message, state: FSMContext):
    dt = parse_custom_date(message.text)
    if not dt:
        await message.reply("❌ <b>Invalid format!</b>\nPlease try again (e.g., <code>27/02/2032 8:30 pm</code>).", parse_mode="HTML")
        return
    if dt < datetime.now():
        await message.reply("⏳ <b>Time Travel not supported!</b>\nThe time must be in the future. Please try again.", parse_mode="HTML")
        return
        
    await state.update_data(start_time=dt.isoformat())
    await message.reply("🎁 <b>Step 2: Reward Message</b>\n\nSend a text message for the winners (e.g., how to claim rewards).\nOr simply type <code>skip</code> if there are no rewards.", parse_mode="HTML")
    await state.set_state(QuizForm.reward_msg)

@router.message(QuizForm.reward_msg)
async def process_reward(message: types.Message, state: FSMContext):
    msg = message.html_text if message.text.lower() != 'skip' else None
    await state.update_data(reward_msg=msg)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⏱ {i} sec", callback_data=f"time_{i}") for i in [10, 20, 30]],
        [InlineKeyboardButton(text=f"⏱ {i} sec", callback_data=f"time_{i}") for i in [40, 50, 60]]
    ])
    await message.reply("⏱ <b>Step 3: Time Per Question</b>\n\nSelect how long users have to answer each question (Max 60s):", reply_markup=kb, parse_mode="HTML")
    await state.set_state(QuizForm.time_per_q)

@router.callback_query(QuizForm.time_per_q)
async def process_time_q(callback: types.CallbackQuery, state: FSMContext):
    t = int(callback.data.split("_")[1])
    await state.update_data(time_per_q=t)
    await callback.message.edit_text(f"✅ Selected: <b>{t} seconds</b> per question.\n\n📝 <b>Step 4: Create Questions</b>\n\nPlease send the text for your <b>FIRST QUESTION</b>:", parse_mode="HTML")
    await state.set_state(QuizForm.q_text)

@router.message(QuizForm.q_text)
async def process_q_text(message: types.Message, state: FSMContext):
    await state.update_data(current_q=message.html_text)
    await message.reply("🅰️ Send <b>Option 1</b>:", parse_mode="HTML")
    await state.set_state(QuizForm.opt1)

@router.message(QuizForm.opt1)
async def p_opt1(m: types.Message, state: FSMContext): 
    await state.update_data(opt1=m.text); await m.reply("🅱️ Send <b>Option 2</b>:", parse_mode="HTML"); await state.set_state(QuizForm.opt2)

@router.message(QuizForm.opt2)
async def p_opt2(m: types.Message, state: FSMContext): 
    await state.update_data(opt2=m.text); await m.reply("©️ Send <b>Option 3</b>:", parse_mode="HTML"); await state.set_state(QuizForm.opt3)

@router.message(QuizForm.opt3)
async def p_opt3(m: types.Message, state: FSMContext): 
    await state.update_data(opt3=m.text); await m.reply("🅳 Send <b>Option 4</b>:", parse_mode="HTML"); await state.set_state(QuizForm.opt4)

@router.message(QuizForm.opt4)
async def p_opt4(message: types.Message, state: FSMContext):
    await state.update_data(opt4=message.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣", callback_data="corr_1"), InlineKeyboardButton(text="2️⃣", callback_data="corr_2")],
        [InlineKeyboardButton(text="3️⃣", callback_data="corr_3"), InlineKeyboardButton(text="4️⃣", callback_data="corr_4")]
    ])
    await message.reply("🎯 <b>Which option is correct?</b>\nSelect the right answer below:", reply_markup=kb, parse_mode="HTML")
    await state.set_state(QuizForm.correct_opt)

@router.callback_query(QuizForm.correct_opt)
async def process_correct_opt(callback: types.CallbackQuery, state: FSMContext):
    corr = int(callback.data.split("_")[1])
    data = await state.get_data()
    
    q_dict = {
        'q_text': data['current_q'],
        'options': [data['opt1'], data['opt2'], data['opt3'], data['opt4']],
        'correct_idx': corr - 1
    }
    questions = data.get('questions', [])
    questions.append(q_dict)
    await state.update_data(questions=questions)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Another Question", callback_data="next_q")],
        [InlineKeyboardButton(text="🛑 Stop & Finalize", callback_data="stop_q")]
    ])
    await callback.message.edit_text(f"✅ <b>Question saved!</b>\nTotal questions added: <b>{len(questions)}</b>", reply_markup=kb, parse_mode="HTML")
    await state.set_state(QuizForm.next_action)

@router.callback_query(QuizForm.next_action)
async def process_next_action(callback: types.CallbackQuery, state: FSMContext):
    if callback.data == "next_q":
        await callback.message.edit_text("📝 Please send the text for your <b>NEXT QUESTION</b>:", parse_mode="HTML")
        await state.set_state(QuizForm.q_text)
    else:
        text = (
            "📢 <b>Step 5: Notification Message</b>\n\n"
            "Send the message to broadcast to the group during reminders.\n"
            "<i>Example: 'Join the grand quiz! The 1st and 2nd winners will get special rewards!'</i>"
        )
        await callback.message.edit_text(text, parse_mode="HTML")
        await state.set_state(QuizForm.notif_msg)

@router.message(QuizForm.notif_msg)
async def process_notif(message: types.Message, state: FSMContext):
    await state.update_data(notif_msg=message.html_text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Generate with AI (Gemini)", callback_data="sol_ai")],
        [InlineKeyboardButton(text="✍️ Set Manually", callback_data="sol_man")]
    ])
    await message.reply("💡 <b>Step 6: Solutions</b>\n\nHow would you like to provide the solutions for this quiz?", reply_markup=kb, parse_mode="HTML")
    await state.set_state(QuizForm.solution_type)

@router.callback_query(QuizForm.solution_type)
async def process_sol_type(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if callback.data == "sol_man":
        await callback.message.edit_text("✍️ <b>Manual Solutions</b>\n\nPlease send all explanations in <b>ONE single message</b>.", parse_mode="HTML")
        await state.set_state(QuizForm.manual_solution)
    else:
        await callback.message.edit_text("🤖 <i>Generating beautifully detailed AI solutions... Please wait.</i>", parse_mode="HTML")
        
        # Build prompt for AI
        prompt = "Provide a short but highly detailed and accurate solution for the following quiz questions. Use clear HTML formatting (<b>, <i>, <code>). Do not use Markdown.\n\n"
        for i, q in enumerate(data['questions']):
            prompt += f"Question {i+1}: {q['q_text']}\nOptions: {', '.join(q['options'])}\nCorrect Answer: {q['options'][q['correct_idx']]}\n\n"
        
        sol_text = await generate_gemini_solution(prompt)
        
        await bot.send_message(callback.from_user.id, f"✅ <b>AI Solutions Generated:</b>\n\n{sol_text}", parse_mode="HTML")
        await finalize_and_save_quiz(data, sol_text, callback.from_user.id, callback.message)
        await state.clear()

@router.message(QuizForm.manual_solution)
async def process_manual_sol(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await finalize_and_save_quiz(data, message.html_text, message.from_user.id, message)
    await state.clear()

async def finalize_and_save_quiz(data, solution, admin_id, context_msg):
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
        
        # Start the background task for this specific quiz
        task = asyncio.create_task(quiz_lifecycle_manager(quiz.id))
        active_quiz_tasks[quiz.id] = task
        
    success_text = (
        "🎉 <b>Quiz Successfully Scheduled!</b>\n\n"
        f"📅 <b>Start Time:</b> {start_time.strftime('%d/%m/%Y %I:%M %p')}\n"
        f"⏱ <b>Per Question:</b> {data['time_per_q']}s\n"
        f"📊 <b>Total Questions:</b> {len(data['questions'])}\n\n"
        "<i>Automated reminders have been set and the quiz will run flawlessly!</i>"
    )
    if isinstance(context_msg, types.Message):
        await context_msg.reply(success_text, parse_mode="HTML")
    else: # Callback Query Message
        await context_msg.edit_text(success_text, parse_mode="HTML")

# ================= BACKGROUND SCHEDULER & LIFECYCLE ================= #
async def load_scheduled_quizzes():
    """Runs on bot startup to recover any pending quizzes from DB."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Quiz).where(Quiz.status == 'scheduled'))
        quizzes = result.scalars().all()
        for quiz in quizzes:
            if quiz.id not in active_quiz_tasks:
                task = asyncio.create_task(quiz_lifecycle_manager(quiz.id))
                active_quiz_tasks[quiz.id] = task
        logging.info(f"Loaded {len(quizzes)} scheduled quizzes into memory.")

async def quiz_lifecycle_manager(quiz_id: int):
    """Manages the lifecycle, notifications, and execution of a single quiz."""
    try:
        async with AsyncSessionLocal() as session:
            quiz = await session.get(Quiz, quiz_id)
            if not quiz: return
            start_time = quiz.start_time
            group_id = quiz.group_id
            notif_msg = quiz.notif_msg

        notified_milestones = set()

        while True:
            now = datetime.now()
            time_left = (start_time - now).total_seconds()

            if time_left <= 0:
                break # Time to start the quiz!

            # Hourly Notifications logic (Only notify if exactly on an hour mark, or close to it)
            hours_left = time_left / 3600
            if hours_left > 1:
                # If we cross an exact hour boundary, send notif. We check every 60s.
                nearest_hour = int(hours_left)
                milestone_key = f"hour_{nearest_hour}"
                if abs(hours_left - nearest_hour) < 0.02 and milestone_key not in notified_milestones: # within ~1 minute of boundary
                    await send_group_notif(group_id, f"📢 <b>Reminder:</b>\n{notif_msg}\n\n<i>Quiz starts in ~{nearest_hour} hours!</i>")
                    notified_milestones.add(milestone_key)
            
            # Minute Countdowns
            mins_left = int(time_left / 60)
            for m in [15, 10, 5, 2, 1]:
                milestone_key = f"min_{m}"
                if mins_left == m and milestone_key not in notified_milestones:
                    if m > 1:
                        text = f"📢 <b>Quiz Alert!</b>\n{notif_msg}\n\n⏳ <b>Starting in exactly {m} minutes!</b> Be ready!"
                    else:
                        text = f"🔥 <b>BE READY!</b>\n\nThe quiz is starting in <b>1 MINUTE!</b> 🚀"
                    await send_group_notif(group_id, text)
                    notified_milestones.add(milestone_key)

            await asyncio.sleep(min(time_left, 30)) # Sleep dynamically to save CPU

        # Start Execution
        await execute_quiz(quiz_id)

    except Exception as e:
        logging.error(f"Error in lifecycle manager for quiz {quiz_id}: {e}")
    finally:
        if quiz_id in active_quiz_tasks:
            del active_quiz_tasks[quiz_id]

async def send_group_notif(group_id: int, text: str):
    try:
        await bot.send_message(group_id, text, parse_mode="HTML")
    except Exception as e:
        logging.warning(f"Failed to send notif to {group_id}: {e}")

# ================= QUIZ EXECUTION (ANTI-CHEAT) ================= #
async def execute_quiz(quiz_id: int):
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

    await bot.send_message(group_id, "🚀 <b>THE QUIZ HAS STARTED!</b>\nRead carefully, you only get one vote per question!", parse_mode="HTML")

    for i, q in enumerate(questions):
        kb_buttons = []
        # Arrange options in rows of 1 or 2 depending on text length. Using 1 per row for safety.
        for idx, opt in enumerate(q.options):
            kb_buttons.append([InlineKeyboardButton(text=opt, callback_data=f"v_{quiz_id}_{q.id}_{idx}")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
        
        q_text = (
            f"❓ <b>Question {i+1}/{len(questions)}</b>\n\n"
            f"<b>{q.q_text}</b>\n\n"
            f"<i>⏱ Time limit: {quiz.time_per_q} seconds</i>"
        )
        
        msg = await bot.send_message(
            group_id, 
            q_text, 
            reply_markup=kb, 
            parse_mode="HTML", 
            protect_content=True # CRITICAL ANTI-CHEAT: Prevents screenshots/forwarding
        )
        
        await asyncio.sleep(quiz.time_per_q)
        
        # Close voting silently
        try:
            await bot.edit_message_reply_markup(chat_id=group_id, message_id=msg.message_id, reply_markup=None)
        except Exception:
            pass # Message might be deleted
        
        await asyncio.sleep(2) # Brief buffer before next question
        
        # Tally scores internally without announcing
        q_votes = active_votes_cache[quiz_id].get(q.id, {})
        for uid, user_data in q_votes.items():
            choice = user_data['choice']
            user_names[uid] = user_data['name']
            if choice == q.correct_idx:
                scores[uid] = scores.get(uid, 0) + 1

    # End Quiz and Process Results
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

    await bot.send_message(group_id, lb_text, parse_mode="HTML")
    
    if quiz.reward_msg:
        await asyncio.sleep(1)
        await bot.send_message(group_id, f"🎁 <b>Rewards Info:</b>\n\n{quiz.reward_msg}", parse_mode="HTML")
        
    if quiz.solution_msg:
        await asyncio.sleep(2)
        await bot.send_message(group_id, f"💡 <b>Detailed Solutions:</b>\n\n{quiz.solution_msg}", parse_mode="HTML")

    # Cleanup memory cache
    if quiz_id in active_votes_cache:
        del active_votes_cache[quiz_id]

@router.callback_query(F.data.startswith("v_"))
async def handle_vote(callback: types.CallbackQuery):
    _, qz_id_str, q_id_str, opt_idx_str = callback.data.split("_")
    qz_id, q_id, opt_idx = int(qz_id_str), int(q_id_str), int(opt_idx_str)
    uid = callback.from_user.id
    name = callback.from_user.first_name
    
    if qz_id not in active_votes_cache:
        await callback.answer("⏳ This quiz is no longer active!", show_alert=True)
        return
        
    if q_id not in active_votes_cache[qz_id]:
        active_votes_cache[qz_id][q_id] = {}
        
    if uid in active_votes_cache[qz_id][q_id]:
        await callback.answer("⚠️ You have already cast your vote for this question!", show_alert=True)
        return
        
    # SILENT VOTE RECORDING (ANTI-ALT ACCOUNT CHEAT)
    # The user gets a popup, but NO indicator if they are right or wrong.
    active_votes_cache[qz_id][q_id][uid] = {'choice': opt_idx, 'name': name}
    await callback.answer("✅ Vote recorded secretly! 🤫", show_alert=False)

# ================= APP STARTUP & WEBHOOK ================= #
async def on_startup(bot: Bot):
    # Initialize Database Tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Load interrupted scheduled quizzes
    await load_scheduled_quizzes()
    
    # Set Webhook for Render compatibility
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"✅ Webhook active at: {WEBHOOK_URL}")

async def on_shutdown(bot: Bot):
    await bot.delete_webhook(drop_pending_updates=True)
    await engine.dispose()
    
    # Cancel all running background tasks gracefully
    for task in active_quiz_tasks.values():
        task.cancel()

def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    # Starts Aiohttp Web Server for Aiogram Webhooks
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)

if __name__ == "__main__":
    main()
