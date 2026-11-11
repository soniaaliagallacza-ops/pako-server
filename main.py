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

META_TOKEN = os.getenv("META_TOKEN")
META_PHONE_ID = os.getenv("META_PHONE_ID")
WEBHOOK_VERIFY = os.getenv("WEBHOOK_VERIFY_TOKEN", "pako_apk_2024")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DB_ID = os.getenv("NOTION_DB_ID", "2ee9af5d941d8124a435000bc2ebe6c8")

claude = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

PAKO_PROMPT = """Eres PAKO, asistente de ventas de APK Cultural Exchange (agencia peruana de intercambio cultural, fundada 2002).
OBJETIVO: Convertir leads en inscritos.
PROGRAMAS: Work & Travel USA, Internship USA, Trainee USA, Internship Espana, Camp Counselor USA, Teach USA, Au Pair USA.
REGLAS: 1) Calido y humano, no robotico. 2) Una pregunta a la vez. 3) Precio -> propone reunion. 4) Interes real -> agenda 20min. 5) NUNCA menciones IA/agente/robot. 6) Firma: *Equipo APK Cultural Exchange*. 7) Max 4 oraciones.
SENALES DE COMPRA (propone reunion): cuanto cuesta / como aplico / requisitos / cuando empieza / me interesa.
CONTACTO: WA 982 913 827 | apkculturalexchange.com"""


async def buscar_lead(telefono: str) -> dict:
    tel = telefono.replace("+","").replace(" ","").replace("-","")
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as c:
        r = await c.post(url, headers=headers, json={"filter": {"property": "Telefono", "phone_number": {"contains": tel[-9:]}}})
    if r.status_code != 200 or not r.json().get("results"): return {}
    page = r.json()["results"][0]
    props = page["properties"]
    def gt(k):
        p = props.get(k, {}); t = p.get("type","")
        if t == "title": return "".join(x["plain_text"] for x in p.get("title",[]))
        if t == "rich_text": return "".join(x["plain_text"] for x in p.get("rich_text",[]))
        if t == "phone_number": return p.get("phone_number","")
        if t == "select": s = p.get("select"); return s["name"] if s else ""
        if t == "multi_select": return [x["name"] for x in p.get("multi_select",[])]
        if t == "checkbox": return p.get("checkbox", False)
        if t == "status": s = p.get("status"); return s["name"] if s else ""
        return ""
    return {"notion_id": page["id"], "nombre": gt("Nombre Completo"), "programa": gt("Programa de interes"), "estado": gt("Estado del lead"), "premio": gt("Premio Mayor"), "sector": gt("Sector de interes")}


async def actualizar_lead(notion_id: str, nota: str):
    if not notion_id: return
    url = f"https://api.notion.com/v1/pages/{notion_id}"
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as c:
        await c.patch(url, headers=headers, json={"properties": {
            "Estado del lead": {"status": {"name": "En seguimiento"}},
            "Notas de seguimiento": {"rich_text": [{"text": {"content": nota[:2000]}}]}
        }})


def respuesta_pako(mensaje: str, lead: dict) -> str:
    nombre = lead.get("nombre","")
    pnombre = nombre.split()[0].capitalize() if nombre else ""
    programa = lead.get("programa","nuestros programas")
    sector = lead.get("sector","")
    if isinstance(sector, list): sector = " / ".join(sector)
    ctx = f"Lead: {nombre} (llamale {pnombre}), programa: {programa}, sector: {sector}, estado: {lead.get('estado','Nuevo')}, Premio Mayor: {'Si' if lead.get('premio') else 'No'}\nMensaje: \"{mensaje}\"\nResponde en max 4 oraciones, calido, orientado a venta."
    r = claude.messages.create(model="claude-sonnet-4-5", max_tokens=300, system=PAKO_PROMPT, messages=[{"role":"user","content":ctx}])
    return r.content[0].text


async def enviar_wa(telefono: str, mensaje: str):
    url = f"https://graph.facebook.com/v19.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as c:
        await c.post(url, headers=headers, json={"messaging_product":"whatsapp","to":telefono,"type":"text","text":{"body":mensaje}})


@app.get("/webhook")
async def verify(request: Request):
    p = dict(request.query_params)
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == WEBHOOK_VERIFY:
        return PlainTextResponse(p.get("hub.challenge",""))
    raise HTTPException(status_code=403)


@app.post("/webhook")
async def receive(request: Request):
    try:
        body = await request.json()
        msg = body.get("entry",[{}])[0].get("changes",[{}])[0].get("value",{}).get("messages",[])
        if not msg or msg[0].get("type") != "text": return {"status":"ok"}
        tel = msg[0].get("from","")
        texto = msg[0].get("text",{}).get("body","")
        if not tel or not texto: return {"status":"ok"}
        lead = await buscar_lead(tel)
        respuesta = respuesta_pako(texto, lead)
        await enviar_wa(tel, respuesta)
        if lead.get("notion_id"):
            await actualizar_lead(lead["notion_id"], f"[PAKO: '{texto[:80]}']")
    except Exception as e:
        print(f"Error: {e}")
    return {"status":"ok"}


@app.get("/")
async def health():
    return {"status": "PAKO activo", "version": "1.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
