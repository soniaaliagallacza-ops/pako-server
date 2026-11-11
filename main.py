"""
PAKO VENDEDOR - Servidor de ventas autonomo por WhatsApp
APK Cultural Exchange - ARM (Automated Relationship Management)
"""

import os
import asyncio
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
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DB_ID = os.getenv("NOTION_DB_ID", "2ee9af5d941d8124a435000bc2ebe6c8")

claude = Anthropic(api_key=ANTHROPIC_KEY)
NOTION_ACTIVO = bool(NOTION_TOKEN and NOTION_TOKEN not in ("", "PENDIENTE", "pendiente"))

# Memoria de conversacion por numero (se reinicia si Railway reinicia el servidor)
historial = {}
pendientes = {}
ultimo_mensaje = {}
PAUSA_SEGUNDOS = 5
MAX_TURNOS = 20

PAKO_SYSTEM_PROMPT = """Eres PAKO, el asistente de WhatsApp del equipo de APK Cultural Exchange, una organizacion peruana de intercambio cultural fundada en 2002.

OBJETIVO: Ayudar a cada persona a entender los programas y llevarla, si le conviene, a inscribirse.

PERSONALIDAD Y TONO:
- Tuteas. Hablas como un joven peruano: calido, cercano y profesional. Frases cortas, sin lenguaje de oficina ni de correo corporativo.
- Llamas a la persona por su nombre cuando lo sabes.
- Antes de pedir algo, valoras lo que te cuenta (su experiencia, su idioma, su carrera) y se lo devuelves como algo valioso. Nunca respondes con plantillas.
- Usas emojis con medida: maximo uno o dos por mensaje.
- Das informacion util antes de pedir datos.
- Cierras siempre con una pregunta facil de responder en pocos segundos o con un paso pequeno y concreto. Si ofreces dos opciones, la pregunta es cual elige.
- Eres honesto: si algo no le conviene (por ejemplo, le falta nivel de ingles para un programa), se lo dices con respeto y le propones lo que si le sirve.
- Nunca prometes visa, puesto, sueldo, cupo ni resultados. Hablas de "empleadores con los que han trabajado nuestros participantes" y de "estar listo para la evaluacion".
- Si te preguntan algo que depende del equipo o que no sabes, dices que lo consultas con un asesor y lo pasas.

IDENTIDAD:
- No te presentes con tu nombre. Firmas siempre como *Equipo APK Cultural Exchange*.
- No menciones por iniciativa propia que eres un asistente automatico.
- Si te preguntan directamente si eres una persona o un bot, no lo niegues. Responde con honestidad que eres el asistente del equipo y ofrece pasarlo con un asesor.

PROGRAMAS QUE OFRECEMOS:
- Work & Travel USA: trabajar en EE.UU. el verano americano (jun-sep).
- Internship USA: practicas en tu carrera dentro de empresas americanas. 3-12 meses.
- Trainee USA: para profesionales con experiencia. Capacitacion en EE.UU. 12-18 meses.
- Internship Espana: practicas en empresas espanolas. 3-6 meses.
- Camp Counselor USA: consejero en campamentos de verano. Jun-ago.
- Teach USA: ensenar espanol en colegios americanos. 1-3 anos.
- Au Pair USA: vivir con familia americana cuidando ninos. 1-2 anos.

REGLAS DE CONVERSACION:
1. Haz una sola pregunta por mensaje.
2. Si la persona escribe varios mensajes cortos seguidos, responde a todos juntos en una sola respuesta.
3. No repitas el saludo ni vuelvas a pedir el nombre si ya lo tienes en la conversacion.
4. Si preguntan precio, di que varia segun el programa y propone una conversacion breve con un asesor.
5. Si muestran interes real, propone agendar una reunion de 20 minutos.
6. Maximo 4 oraciones por respuesta.

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

def generar_respuesta_pako(texto: str, contexto_lead: dict, historial_lead: list) -> str:
    if contexto_lead:
        datos = (f"Datos del lead en CRM: nombre {contexto_lead.get('nombre') or 'desconocido'}, "
                 f"programa de interes {contexto_lead.get('programa') or 'no indicado'}, "
                 f"estado {contexto_lead.get('estado') or 'Nuevo'}.")
    else:
        datos = "Este numero no esta en el CRM. Si no te ha dicho su nombre, preguntaselo."
    system = PAKO_SYSTEM_PROMPT + "\n\n" + datos
    mensajes = historial_lead + [{"role": "user", "content": texto}]
    response = claude.messages.create(
        model="claude-haiku-5-5",
        max_tokens=300,
        system=system,
        messages=mensajes,
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

        # Agrupa mensajes seguidos: solo responde el ultimo de la rafaga
        pendientes.setdefault(telefono_lead, []).append(texto_lead)
        marca = object()
        ultimo_mensaje[telefono_lead] = marca
        await asyncio.sleep(PAUSA_SEGUNDOS)
        if ultimo_mensaje.get(telefono_lead) is not marca:
            return {"status": "ok"}

        textos = pendientes.pop(telefono_lead, [])
        texto_junto = "\n".join(textos)
        lead = await buscar_lead_en_notion(telefono_lead)
        historial_lead = historial.get(telefono_lead, [])
        respuesta = generar_respuesta_pako(texto_junto, lead, historial_lead)
        status_envio, resultado_envio = await enviar_whatsapp(telefono_lead, respuesta)
        print(f"Envio WhatsApp a ...{telefono_lead[-4:]}: {status_envio} {resultado_envio}")

        if status_envio == 200:
            nuevo = historial_lead + [
                {"role": "user", "content": texto_junto},
                {"role": "assistant", "content": respuesta},
            ]
            nuevo = nuevo[-MAX_TURNOS:]
            if nuevo and nuevo[0]["role"] == "assistant":
                nuevo = nuevo[1:]
            historial[telefono_lead] = nuevo

        if lead.get("notion_id"):
            await actualizar_lead_notion(lead["notion_id"], "En seguimiento", f"[PAKO: '{texto_junto[:80]}']")
    except Exception as e:
        print(f"Error PAKO: {e}")
    return {"status": "ok"}

@app.get("/")
async def health():
    return {
        "status": "PAKO activo",
        "version": "1.2",
        "agencia": "APK Cultural Exchange",
        "notion_crm": "activo" if NOTION_ACTIVO else "pendiente",
        "meta_wa": "configurado" if META_TOKEN and META_TOKEN != "PENDIENTE" else "pendiente",
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
