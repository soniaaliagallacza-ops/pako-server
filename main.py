"""
PAKO VENDEDOR - Servidor de ventas autonomo por WhatsApp
APK Cultural Exchange - ARM (Automated Relationship Management)
"""

import os
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="PAKO Vendedor - APK ARM")

META_TOKEN        = os.getenv("META_TOKEN")
META_PHONE_ID     = os.getenv("META_PHONE_ID")
WEBHOOK_VERIFY    = os.getenv("WEBHOOK_VERIFY_TOKEN", "pako_apk_2024")
ANTHROPIC_KEY     = os.getenv("ANTHROPIC_API_KEY")
NOTION_TOKEN      = os.getenv("NOTION_TOKEN")
NOTION_DB_ID      = os.getenv("NOTION_DB_ID", "2ee9af5d941d8124a435000bc2ebe6c8")

claude = Anthropic(api_key=ANTHROPIC_KEY)
NOTION_ACTIVO = bool(NOTION_TOKEN and NOTION_TOKEN not in ("", "PENDIENTE", "pendiente"))

PAKO_SYSTEM_PROMPT = """Eres PAKO, el asistente de ventas de APK Cultural Exchange,
una agencia peruana de intercambio cultural fundada en 2002.

TU OBJETIVO: Convertir leads interesados en inscritos a nuestros programas.

PROGRAMAS QUE OFRECEMOS:
- Work & Travel USA: trabajar en EE.UU. el verano americano (jun-sep).
- Internship USA: practicas en tu carrera dentro de empresas americanas. 3-12 meses.
- Trainee USA: para profesionales con experiencia. Capacitacion en EE.UU. 12-18 meses.
- Internship Espana: practicas en empresas espanolas. 3-6 meses.
- Camp Counselor USA: consejero en campamentos de verano. Jun-ago.
- Teach USA: ensenar espanol en colegios americanos. 1-3 anos.
- Au Pair USA: vivir con familia americana cuidando ninos. 1-2 anos.

REGLAS:
1. Se calido, directo y humano. No suenes como robot.
2. Haz UNA sola pregunta a la vez.
3. Si preguntan el precio, di que varia segun el programa y propone una reunion.
4. Si muestran interes real, invitalos a agendar una reunion de 20 minutos.
5. NUNCA menciones que eres un agente automatico, IA o robot.
6. SIEMPRE firma como: *Equipo APK Cultural Exchange*
7. Maximo 3-4 oraciones por respuesta.

CONTACTO: WhatsApp APK: 982 913 827 | Web: apkculturalexchange.com"""


async def buscar_lead_en_notion(telefono: str) -> dict:
    if not NOTION_ACTIVO:
        return {}
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    tel_limpio = telefono.replace("+", "").replace(" ", "").replace("-", "")
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json={
            "filter": {"property": "Telefono", "phone_number": {"contains": tel_limpio[-9:]}}
        })
    if resp.status_code != 200:
        return {}
    results = resp.json().get("results", [])
    if not results:
        return {}
    page = results[0]
    props = page.get("properties", {})

    def get_text(prop):
        p = props.get(prop, {})
        t = p.get("type")
        if t == "title": return "".join(r.get("plain_text","") for r in p.get("title",[]))
        if t == "rich_text": return "".join(r.get("plain_text","") for r in p.get("rich_text",[]))
        if t == "phone_number": return p.get("phone_number","")
        if t == "select":
            s = p.get("select"); return s.get("name","") if s else ""
        if t == "status":
            s = p.get("status"); return s.get("name","") if s else ""
        return ""

    return {
        "notion_id": page["id"],
        "nombre": get_text("Nombre Completo"),
        "programa": get_text("Programa de interes"),
        "estado": get_text("Estado del lead"),
    }


async def actualizar_lead_notion(notion_id: str, estado: str = None, nota: str = None):
    if not notion_id or not NOTION_ACTIVO:
        return
    url = f"https://api.notion.com/v1/pages/{notion_id}"
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    props = {}
    if estado: props["Estado del lead"] = {"status": {"name": estado}}
    if nota: props["Notas de seguimiento"] = {"rich_text": [{"text": {"content": nota[:2000]}}]}
    async with httpx.AsyncClient() as client:
        await client.patch(url, headers=headers, json={"properties": props})


def generar_respuesta_pako(mensaje_lead: str, contexto_lead: dict) -> str:
    nombre = contexto_lead.get("nombre", "")
    primer_nombre = nombre.split()[0].capitalize() if nombre else ""
    programa = contexto_lead.get("programa", "nuestros programas")

    if contexto_lead:
        ctx = f"""Contexto del lead:
- Nombre: {nombre} (llamale {primer_nombre})
- Programa de interes: {programa}
- Estado: {contexto_lead.get('estado', 'Nuevo')}

Mensaje: "{mensaje_lead}"
Genera respuesta calida y orientada a venta. Max 4 oraciones."""
    else:
        ctx = f"""Lead nuevo sin datos en CRM.
Mensaje: "{mensaje_lead}"
Responde de forma calida. Pregunta su nombre si no lo menciono. Max 4 oraciones."""

    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=300,
        system=PAKO_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": ctx}]
    )
    return response.content[0].text


async def enviar_whatsapp(telefono_destino: str, mensaje: str):
    url = f"https://graph.facebook.com/v19.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": telefono_destino, "type": "text", "text": {"body": mensaje}}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=payload)
    return resp.status_code, resp.json()


@app.get("/webhook")
async def verificar_webhook(request: Request):
    params = dict(request.query_params)
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == WEBHOOK_VERIFY:
        return PlainTextResponse(params.get("hub.challenge"))
    raise HTTPException(status_code=403, detail="Token invalido")


@app.post("/webhook")
async def recibir_mensaje(request: Request):
    try:
        body = await request.json()
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return {"status": "ok"}
        msg = messages[0]
        telefono_lead = msg.get("from", "")
        if msg.get("type") != "text":
            return {"status": "ok"}
        texto_lead = msg.get("text", {}).get("body", "")
        if not texto_lead or not telefono_lead:
            return {"status": "ok"}
        lead = await buscar_lead_en_notion(telefono_lead)
        respuesta = generar_respuesta_pako(texto_lead, lead)
        await enviar_whatsapp(telefono_lead, respuesta)
        if lead.get("notion_id"):
            await actualizar_lead_notion(lead["notion_id"], "En seguimiento", f"[PAKO: '{texto_lead[:80]}']")
    except Exception as e:
        print(f"Error PAKO: {e}")
    return {"status": "ok"}


@app.get("/")
async def health():
    return {
        "status": "PAKO activo",
        "version": "1.1",
        "agencia": "APK Cultural Exchange",
        "notion_crm": "activo" if NOTION_ACTIVO else "pendiente",
        "meta_wa": "configurado" if META_TOKEN and META_TOKEN != "PENDIENTE" else "pendiente",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
