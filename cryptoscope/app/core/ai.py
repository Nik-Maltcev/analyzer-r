"""DeepSeek AI analysis client."""

import httpx
from typing import Dict, Any, Optional


DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
SYSTEM_PROMPTS = {
    "ru": (
        "Ты профессиональный аналитик парного трейдинга. Анализируй "
        "коинтеграцию, Z-score и корреляции. Отвечай на русском языке, "
        "конкретно и без воды. Используй markdown."
    ),
    "en": (
        "You are a professional pairs trading analyst. Analyze cointegration, "
        "Z-scores and correlations. Answer in English, be concise and "
        "specific, and use markdown."
    ),
    "pt-BR": (
        "Você é um analista profissional de pairs trading. Analise "
        "cointegração, Z-score e correlações. Responda em português do Brasil, "
        "de forma objetiva e específica, usando markdown."
    ),
    "id": (
        "Anda adalah analis pairs trading profesional. Analisis kointegrasi, "
        "Z-score, dan korelasi. Jawab dalam bahasa Indonesia secara ringkas "
        "dan spesifik dengan format markdown."
    ),
}

PROMPT_COPY = {
    "ru": {
        "intro": "Проанализируй текущую ситуацию на рынке",
        "active": "Активные сигналы (pairs trading)",
        "top": "Топ пар по рейтингу",
        "strength": "сила",
        "yes": "да",
        "no": "нет",
        "questions": """Ответь на вопросы:
1. Какую сделку лучше открыть прямо сейчас? (конкретная пара, направление)
2. Когда входить? (уровень Z-score)
3. Когда выходить? (целевой уровень и время удержания)
4. Какой размер позиции использовать? (% от капитала)
5. Каковы основные риски?
6. Какой есть альтернативный вариант?

Формат: markdown, не более 500 слов. Будь конкретен.""",
    },
    "en": {
        "intro": "Analyze the current market situation for",
        "active": "Active pairs trading signals",
        "top": "Top-ranked pairs",
        "strength": "strength",
        "yes": "yes",
        "no": "no",
        "questions": """Answer these questions:
1. Which trade is best right now? (specific pair and direction)
2. When should it be entered? (Z-score level)
3. When should it be exited? (target and holding time)
4. What position size should be used? (% of capital)
5. What are the main risks?
6. What is the alternative?

Use markdown, stay under 500 words, and be specific.""",
    },
    "pt-BR": {
        "intro": "Analise a situação atual do mercado",
        "active": "Sinais ativos de pairs trading",
        "top": "Pares mais bem classificados",
        "strength": "força",
        "yes": "sim",
        "no": "não",
        "questions": """Responda:
1. Qual operação é mais indicada agora? (par e direção)
2. Quando entrar? (nível de Z-score)
3. Quando sair? (alvo e tempo de manutenção)
4. Qual tamanho de posição usar? (% do capital)
5. Quais são os principais riscos?
6. Qual é a alternativa?

Use markdown, no máximo 500 palavras, e seja específico.""",
    },
    "id": {
        "intro": "Analisis situasi pasar saat ini untuk",
        "active": "Sinyal pairs trading aktif",
        "top": "Pasangan dengan peringkat teratas",
        "strength": "kekuatan",
        "yes": "ya",
        "no": "tidak",
        "questions": """Jawab pertanyaan berikut:
1. Trade mana yang terbaik saat ini? (pasangan dan arah)
2. Kapan masuk? (level Z-score)
3. Kapan keluar? (target dan waktu hold)
4. Berapa ukuran posisi yang sesuai? (% modal)
5. Apa risiko utamanya?
6. Apa alternatifnya?

Gunakan markdown, maksimal 500 kata, dan berikan jawaban spesifik.""",
    },
}


def build_analysis_prompt(
    context: Dict[str, Any],
    locale: str = "ru",
) -> str:
    """Build a structured analysis prompt from market context."""
    market = context.get("market", "crypto")
    active_signals = context.get("active_signals", [])
    top_pairs = context.get("top_pairs", [])
    
    copy = PROMPT_COPY.get(locale, PROMPT_COPY["en"])
    prompt = f"{copy['intro']} **{market}**.\n\n"
    
    if active_signals:
        prompt += f"### {copy['active']}:\n"
        for s in active_signals[:10]:
            z = s.get("z_now", "N/A")
            zf = s.get("z_forecast", "N/A")
            prompt += (
                f"- {s['ticker_a']}/{s['ticker_b']}: "
                f"{s.get('signal', 'N/A')}, Z={z}, Z forecast={zf}, "
                f"{copy['strength']}={s.get('strength', 'N/A')}\n"
            )
    
    if top_pairs:
        prompt += f"\n### {copy['top']}:\n"
        for p in top_pairs[:5]:
            coint = copy["yes"] if p.get("is_coint") else copy["no"]
            prompt += (
                f"- {p['ticker_a']}/{p['ticker_b']}: "
                f"corr={p.get('corr', 'N/A')}, coint={coint}, "
                f"score={p.get('score', 'N/A')}\n"
            )
    
    prompt += f"\n{copy['questions']}\n"
    return prompt


async def call_deepseek(api_key: str, prompt: str, model: str = "deepseek-v4-pro",
                        max_tokens: int = 1500, temperature: float = 0.3,
                        timeout: int = 120, locale: str = "ru") -> Dict[str, Any]:
    """Call DeepSeek API and return response."""
    if not api_key:
        return {"error": "DEEPSEEK_API_KEY не настроен", "response": None, "tokens": None}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPTS.get(locale, SYSTEM_PROMPTS["en"]),
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(DEEPSEEK_API_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            
            return {
                "error": None,
                "response": content,
                "tokens": {
                    "prompt": usage.get("prompt_tokens", 0),
                    "completion": usage.get("completion_tokens", 0),
                    "total": usage.get("total_tokens", 0),
                },
            }
    except httpx.HTTPStatusError as e:
        return {"error": f"API error {e.response.status_code}: {e.response.text[:200]}", "response": None, "tokens": None}
    except Exception as e:
        return {"error": str(e), "response": None, "tokens": None}
