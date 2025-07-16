import subprocess
import os
import re
import json
import asyncio
import aiohttp
from dotenv import load_dotenv
from aiolimiter import AsyncLimiter

# ========== 配置区 ==========
load_dotenv()
API_KEY = os.getenv("OPENAI_API_KEY")
API_BASE = os.getenv("OPENAI_API_BASE")

MODEL = "gpt-4.1-nano"
INPUT_FILE = "/data/1.txt"
OUTPUT_DIR = "/data/output"
CHECKPOINT_FILE = "/data/checkpoint.json"
TSV_FILE = "/data/result3.txt"
MAX_CHAPTERS = None  # 设置为整数只处理部分章节，None 表示处理全部
RATE_LIMIT = 10  # 每分钟最多请求次数（10 RPM）
CHAPTER_PATTERN = r'(第\d+章\s+.*?（.*?）)\s*\n'

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ========== 工具函数 ==========
def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_checkpoint(data):
    with open(CHECKPOINT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def notify_mac(title, message):
    subprocess.run([
        "osascript", "-e",
        f'display notification "{message}" with title "{title}"'
    ])

async def call_openai_format(session, limiter, title, body, chapter_num, max_retries=5):
    prompt = (
            "请将以下文本重新自然分段，每段不超过40个汉字，并保持语义完整。"
            "每段换行输出，不添加任何前缀或注释：\n\n" + body
    )
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "你是一个专业的中文网络小说编辑助手。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2
    }

    numbered_title = f"{chapter_num:04d} {title}"
    output_path = os.path.join(OUTPUT_DIR, f"{numbered_title}.txt")

    for attempt in range(1, max_retries + 1):
        try:
            async with limiter:
                async with session.post(f"{API_BASE}/chat/completions", headers=headers, json=payload, timeout=60) as resp:
                    if resp.status == 429:
                        print(f"⚠️ 429 限流：等待 {30 * attempt}s 后重试（第 {attempt}/{max_retries} 次）")
                        await asyncio.sleep(30 * attempt)
                        continue
                    elif resp.status != 200:
                        text = await resp.text()
                        raise Exception(f"HTTP {resp.status}: {text}")
                    data = await resp.json()
                    result = data['choices'][0]['message']['content'].strip()

                    with open(output_path, 'w', encoding='utf-8') as f_out:
                        f_out.write(title + '\n\n' + result)

                    safe_result = result.replace('\n', '\\n')
                    print(f"✅ 完成章节：{numbered_title}")
                    return (title, f"{title}\t{safe_result}")

        except Exception as e:
            print(f"❌ 错误章节 [{numbered_title}] 第 {attempt}/{max_retries} 次尝试失败: {e}")
            if attempt == max_retries:
                return None
            await asyncio.sleep(5 * attempt)

# ========== 主流程 ==========
async def split_and_process_async():
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        content = f.read()

    parts = re.split(CHAPTER_PATTERN, content)
    checkpoint = load_checkpoint()
    limiter = AsyncLimiter(RATE_LIMIT, 60)  # 每60秒最多10次请求

    tasks = []
    chapter_num = 0

    async with aiohttp.ClientSession() as session:
        for i in range(1, len(parts), 2):
            title = parts[i].strip()
            body = parts[i+1].strip().replace('\n', ' ')

            if title in checkpoint:
                print(f"⏩ 跳过已完成章节：{title}")
                continue

            chapter_num += 1
            if MAX_CHAPTERS and chapter_num > MAX_CHAPTERS:
                break

            tasks.append(call_openai_format(session, limiter, title, body, chapter_num))

        results = await asyncio.gather(*tasks)

        for result in results:
            if result:
                title, _ = result
                checkpoint[title] = "done"

        save_checkpoint(checkpoint)

    await regenerate_tsv_from_output()
    notify_mac("处理完成", "已重新整理写入所有章节到 TSV 文件")

# ========== 重建 TSV ==========
async def regenerate_tsv_from_output():
    entries = []
    for filename in sorted(os.listdir(OUTPUT_DIR)):
        if filename.endswith(".txt"):
            path = os.path.join(OUTPUT_DIR, filename)
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                original_title = lines[0].strip()[4:]  # 原始标题
                content = ''.join(lines[2:]).strip()  # 正文内容
                entries.append(f"{original_title}\n{content}\n")

    with open(TSV_FILE, 'w', encoding='utf-8') as f:
        for line in entries:
            f.write(line + '\n')
    print(f"📝 已重建 TSV 文件，共 {len(entries)} 章")

# ========== 执行 ==========
if __name__ == '__main__':
    asyncio.run(split_and_process_async())
