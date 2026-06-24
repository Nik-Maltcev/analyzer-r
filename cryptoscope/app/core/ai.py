"""DeepSeek AI analysis client."""

import httpx
from typing import Dict, Any, Optional


DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
SYSTEM_PROMPT = """Ты — профессиональный криптотрейдер и аналитик с 10-летним опытом. 
Твоя специализация — pairs trading (торговля парами) на криптовалютных, фондовых и валютных рынках.
Ты анализируешь данные о коинтеграции, Z-оценках, корреляциях и даёшь конкретные торговые рекомендации.
Отвечай на русском языке, конкретно и без воды. Используй markdown для форматирования."""


def build_analysis_prompt(context: Dict[str, Any]) -> str:
    """Build a structured analysis prompt from market context."""
    market = context.get("market", "crypto")
    active_signals = context.get("active_signals", [])
    top_pairs = context.get("top_pairs", [])
    
    prompt = f"Проанализируй текущую ситуацию на рынке **{market}**.\n\n"
    
    if active_signals:
        prompt += "### Активные сигналы (pairs trading):\n"
        for s in active_signals[:10]:
            z = s.get("z_now", "N/A")
            zf = s.get("z_forecast", "N/A")
            prompt += f"- {s['ticker_a']}/{s['ticker_b']}: {s.get('signal', 'N/A')}, Z={z}, Z_прогноз={zf}, сила={s.get('strength', 'N/A')}\n"
    
    if top_pairs:
        prompt += "\n### Топ пар по рейтингу:\n"
        for p in top_pairs[:5]:
            prompt += f"- {p['ticker_a']}/{p['ticker_b']}: corr={p.get('corr', 'N/A')}, coint={'да' if p.get('is_coint') else 'нет'}, score={p.get('score', 'N/A')}\n"
    
    prompt += """
Ответь на вопросы:
1. Какую сделку лучше открыть прямо сейчас? (конкретная пара, направление)
2. Когда входить? (уровень Z-score)
3. Когда выходить? (целевой уровень + время удержания)
4. Размер позиции (% от капитала)
5. Основные риски
6. Альтернативный вариант

Формат: markdown, не более 500 слов. Будь конкретен.
"""
    return prompt


async def call_deepseek(api_key: str, prompt: str, model: str = "deepseek-v4-pro",
                        max_tokens: int = 1500, temperature: float = 0.3,
                        timeout: int = 120) -> Dict[str, Any]:
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
            {"role": "system", "content": SYSTEM_PROMPT},
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
