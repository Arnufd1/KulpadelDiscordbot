"""Sigmadvisor's self-hosted portal: Google identity, tenant jobs, private administration."""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import asdict
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .web_store import PlatformStore
from .web_runtime import JobRunner, rules_for, tenant_env
from .web_device import DeviceManager
from .web_cards import CardVault
from .platform_service import PlatformService,install_url


class WebSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file="data/platform/web.env",env_prefix="PADELWEB_",extra="ignore")
    root: Path = Path("data/platform")
    public_url: str = "https://padel.sigmadvisor.com"
    owner_email: str = "aafmichiels@gmail.com"
    google_client_id: str = ""
    google_client_secret: str = ""
    tail_url: str = "https://sigma-minipc.tailad80cf.ts.net:8443"
    tail_proxy_secret: str = ""
    tail_proxy_ip: str = "192.168.0.250"
    tail_admin_node_ids: str = ""
    secure_cookies: bool = True
    card_vault_key_file: Path | None = None
    discord_client_id: str = ''
    discord_client_secret: str = ''


class RangeRequest(BaseModel):
    start: int = Field(default=0,ge=0,le=30)
    end: int = Field(default=5,ge=0,le=30)


class RuleRequest(BaseModel):
    day_of_week: int = Field(ge=0,le=6)
    time_local: str = Field(pattern=r"^\d{2}:\d{2}$")
    end_time_local: str | None = Field(default=None,pattern=r"^\d{2}:\d{2}$")
    notes: str = Field(default="",max_length=100)


class PreferencesRequest(BaseModel):
    automatic_payment: bool = False
    max_amount: float = Field(ge=0,le=100)
    payment_mode: str = Field(default='kul',pattern=r'^(kul|discord)$')


class BookingRequest(BaseModel):
    target: str = Field(max_length=40)
    product: int = Field(gt=0)
    court: int = Field(gt=0)
    amount: float = Field(gt=0,le=100)


class DeviceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: str = Field(pattern=r"^(link|setup_pin|finish|stop)$")
    link: str = Field(default="",max_length=2048)
    pin: str = Field(default="",max_length=12)
    confirm_pin: str = Field(default="",max_length=12)


class QRRequest(BaseModel):
    image: str = Field(max_length=10_666_668)


class CardRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    holder: SecretStr = Field(min_length=1,max_length=80)
    number: SecretStr = Field(min_length=13,max_length=32)
    month: int = Field(ge=1,le=12)
    year: int = Field(ge=2026,le=2100)
    label: str = Field(default='',max_length=40)
    primary: bool = False


class ShareRequest(BaseModel):
    booking_id: int = Field(gt=0)
    shared: bool


class DraftRequest(BaseModel):
    product: int = Field(gt=0)
    court: int = Field(gt=0)
    target: str = Field(max_length=40)
    end: str = Field(max_length=40)


class DraftConfirmRequest(BaseModel):
    amount: float = Field(gt=0,le=100)


def validate_google_claims(claims: dict, nonce: str, audience: str):
    if (claims.get("iss") not in {"https://accounts.google.com","accounts.google.com"}
            or claims.get("aud") != audience or claims.get("email_verified") is not True
            or not isinstance(claims.get("nonce"),str) or not hmac.compare_digest(claims["nonce"],nonce)
            or not claims.get("sub") or not claims.get("email")):
        raise ValueError("Google-identiteit kon niet worden bevestigd.")
    return claims


def create_app(config: WebSettings | None = None, *, start_workers: bool = True) -> FastAPI:
    config = config or WebSettings()
    store = PlatformStore(config.root,config.owner_email)
    runner, devices = JobRunner(store), DeviceManager(store)
    cards = CardVault(store,config.card_vault_key_file)
    community = runner.community
    platform=PlatformService(store,community)
    drafts=runner.drafts
    static = Path(__file__).parent/"web_static"
    keys = jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs",timeout=10)

    @asynccontextmanager
    async def lifespan(app):
        tasks = [asyncio.create_task(fn()) for fn in (runner.run,runner.scheduler,devices.reap)] if start_workers else []
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await devices.close()

    app = FastAPI(title="Sigmadvisor Padel",lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store, app.state.config = store,config
    app.state.cards, app.state.community = cards,community
    app.state.devices=devices
    app.state.platform=platform
    hosts = {urlsplit(config.public_url).hostname,urlsplit(config.tail_url).hostname,"localhost","127.0.0.1","192.168.0.220"}
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=sorted(h for h in hosts if h))
    attempts = {}

    @app.middleware("http")
    async def security(request:Request, call_next):
        # Small per-peer limiter, including on OAuth initiation; reverse proxies are not blindly trusted.
        peer = request.client.host if request.client else "unknown"
        if (config.secure_cookies and peer=="127.0.0.1"
                and request.headers.get("host")==urlsplit(config.public_url).netloc
                and request.headers.get("x-forwarded-proto")=="http"):
            suffix=request.url.path+("?"+request.url.query if request.url.query else "")
            return RedirectResponse(config.public_url+suffix,status_code=308)
        now = time.monotonic()
        count, reset = attempts.get(peer,(0,now+60))
        count = count+1 if now<reset else 1
        reset = reset if now<reset else now+60
        attempts[peer] = (count,reset)
        if len(attempts)>1000:
            for k in list(attempts):
                if attempts[k][1]<now:
                    del attempts[k]
        if count>600:
            return JSONResponse({"detail":"Even wachten: te veel aanvragen."},status_code=429)
        if request.method in {"POST","PUT","PATCH","DELETE"}:
            origin = request.headers.get("origin","")
            if origin != config.public_url:
                return JSONResponse({"detail":"Ongeldige herkomst."},status_code=403)
            session=store.session(request.cookies.get("padel_session",""))
            if not session:
                return JSONResponse({"detail":"Meld je aan met Google."},status_code=401)
            if not hmac.compare_digest(session["csrf"],request.headers.get("x-csrf-token","")):
                return JSONResponse({"detail":"Vernieuw de pagina en probeer opnieuw."},status_code=403)
            length = request.headers.get("content-length")
            limit=10_666_800 if request.url.path=="/api/device/qr" else 16384
            if length and (not length.isdigit() or int(length)>limit):
                return JSONResponse({"detail":"Aanvraag te groot."},status_code=413)
            body=bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body)>limit:
                    return JSONResponse({"detail":"Aanvraag te groot."},status_code=413)
            request._body=bytes(body)
        response = await call_next(request)
        response.headers.update({"X-Content-Type-Options":"nosniff","X-Frame-Options":"DENY",
            "Referrer-Policy":"no-referrer","Cache-Control":"no-store",
            "Permissions-Policy":"camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy":"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.exception_handler(ValueError)
    async def bad_value(request,error):
        return JSONResponse({"detail":str(error)},status_code=400)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,error):
        # Pydantic's default response echoes the invalid input, including PAN/CVC.
        return JSONResponse({'detail':'Controleer de ingevulde gegevens.'},status_code=422)

    def current(request:Request):
        session = store.session(request.cookies.get("padel_session",""))
        if session is None:
            raise HTTPException(401,"Meld je aan met Google.")
        user = store.user(session["user_id"])
        if not user or user["suspended"]:
            raise HTTPException(403,"Dit profiel is gepauzeerd.")
        if request.method not in {"GET","HEAD"} and not hmac.compare_digest(session["csrf"],request.headers.get("x-csrf-token","")):
            raise HTTPException(403,"Vernieuw de pagina en probeer opnieuw.")
        request.state.session = session
        community.hub.register(user)
        return user

    def admin(request:Request,user=Depends(current)):
        if not user["owner"] or request.state.session["admin_until"]<time.time():
            raise HTTPException(403,"Bevestig beheertoegang via Tailscale.")
        return user

    @app.get("/healthz")
    def health():
        return {"status":"ok","google_configured":bool(config.google_client_id and config.google_client_secret)}

    @app.get("/auth/google")
    def google_start():
        if not config.google_client_id or not config.google_client_secret:
            raise HTTPException(503,"Google-login wordt nog ingesteld.")
        state,browser,nonce,verifier = store.oauth_start()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        url = "https://accounts.google.com/o/oauth2/v2/auth?"+urlencode({
            "client_id":config.google_client_id,"redirect_uri":config.public_url+"/auth/google/callback",
            "response_type":"code","scope":"openid email profile","state":state,"nonce":nonce,
            "code_challenge":challenge,"code_challenge_method":"S256","prompt":"select_account"})
        response = RedirectResponse(url,status_code=302)
        response.set_cookie("padel_oauth",browser,max_age=600,httponly=True,secure=config.secure_cookies,samesite="lax",path="/auth")
        return response

    @app.get("/auth/google/callback")
    async def google_callback(request:Request):
        flow = store.oauth_consume(request.query_params.get("state",""),request.cookies.get("padel_oauth",""))
        if not flow or not request.query_params.get("code") or request.query_params.get("error"):
            raise HTTPException(400,"Google-login is verlopen of geweigerd. Probeer opnieuw.")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post("https://oauth2.googleapis.com/token",data={"code":request.query_params["code"],
                    "client_id":config.google_client_id,"client_secret":config.google_client_secret,
                    "redirect_uri":config.public_url+"/auth/google/callback","grant_type":"authorization_code","code_verifier":flow["verifier"]})
                r.raise_for_status()
                token = r.json()["id_token"]
            key = await asyncio.to_thread(keys.get_signing_key_from_jwt,token)
            claims = jwt.decode(token,key.key,algorithms=["RS256"],audience=config.google_client_id,
                options={"require":["exp","iat","iss","aud","sub","nonce","email","email_verified"]})
            validate_google_claims(claims,flow["nonce"],config.google_client_id)
            user = store.login_google(claims)
        except Exception:
            raise HTTPException(400,"Google kon deze aanmelding niet bevestigen. Probeer opnieuw.") from None
        old = request.cookies.get("padel_session","")
        if old:
            store.logout(old)
        token,_ = store.new_session(user["id"])
        response = RedirectResponse(config.public_url+"/",status_code=303)
        response.set_cookie("padel_session",token,max_age=43200,httponly=True,secure=config.secure_cookies,samesite="lax",path="/")
        response.delete_cookie("padel_oauth",path="/auth")
        store.audit(user["id"],"google_login")
        return response

    @app.post("/auth/logout")
    def logout(request:Request,user=Depends(current)):
        store.logout(request.cookies["padel_session"])
        response = JSONResponse({"ok":True})
        response.delete_cookie("padel_session")
        return response

    @app.get('/auth/discord')
    def discord_start(request:Request,personal:bool=False,user=Depends(current)):
        if not config.discord_client_secret:
            return RedirectResponse(install_url(config.discord_client_id,personal=personal),status_code=302)
        state=platform.oauth_start(user['id'],request.cookies['padel_session'])
        return RedirectResponse('https://discord.com/oauth2/authorize?'+urlencode({
            'client_id':config.discord_client_id,'redirect_uri':config.public_url+'/auth/discord/callback',
            'response_type':'code','scope':'identify applications.commands' if personal else 'identify bot applications.commands',
            'integration_type':1 if personal else 0,'permissions':0 if personal else 84992,'state':state}),status_code=302)

    @app.get('/auth/discord/callback')
    async def discord_callback(request:Request,user=Depends(current)):
        if (not config.discord_client_secret or request.query_params.get('error') or
            request.query_params.get('iss','https://discord.com')!='https://discord.com' or
            not platform.oauth_consume(request.query_params.get('state',''),request.cookies['padel_session'],user['id'])):
            raise HTTPException(400,'Discord-autorisatie is verlopen of geweigerd.')
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response=await client.post('https://discord.com/api/oauth2/token',data={
                    'client_id':config.discord_client_id,'client_secret':config.discord_client_secret,
                    'grant_type':'authorization_code','code':request.query_params.get('code',''),
                    'redirect_uri':config.public_url+'/auth/discord/callback'})
                response.raise_for_status();access=response.json()['access_token']
                response=await client.get('https://discord.com/api/v10/users/@me',headers={'Authorization':'Bearer '+access})
                response.raise_for_status();identity=response.json()
                if identity.get('bot'):raise ValueError()
                platform.hub.link(user['id'],int(identity['id']))
                platform.hub.remember_discord(int(identity['id']),identity.get('global_name') or identity['username'])
                # Linking needs no retained Discord access/refresh token.
                try:
                    await client.post('https://discord.com/api/oauth2/token/revoke',data={
                        'client_id':config.discord_client_id,'client_secret':config.discord_client_secret,'token':access})
                except httpx.HTTPError:pass
        except Exception:
            raise HTTPException(400,'Discord kon niet gekoppeld worden. Probeer opnieuw.') from None
        store.audit(user['id'],'discord_linked')
        return RedirectResponse(config.public_url+'/#dashboard',status_code=303)

    @app.get("/api/me")
    def me(request:Request,user=Depends(current)):
        env = tenant_env(store,user)
        connected=Path(env['PADELBOT_TOKEN_FILE']).exists()
        profile=Path(env['PADELBOT_ANDROID_PROFILE_FILE'])
        try:verified=json.loads(profile.read_text()).get('enrollment_verified') is True
        except (OSError,ValueError,AttributeError):verified=False
        return {"id":user["id"],"name":user["name"],"email":user["email"],"owner":bool(user["owner"]),
            "admin":bool(user["owner"] and request.state.session["admin_until"]>time.time()),"csrf":request.state.session["csrf"],
            "connected":connected,"device_ready":profile.exists() and (verified or connected),
            "needs_attention":Path(env["PADELBOT_ANDROID_FAILURE_FILE"]).exists(),
            "discord_linked":community.hub.linked(user['id']),
            "discord_oauth":bool(config.discord_client_secret),
            "discord_install":bool(config.discord_client_id),
            "discord_online":platform.discord_online(),
            "payment":platform.payment_status(user['id']),
            "deliveries":platform.delivery_status(user['id']),
            "preferences":store.preferences(user["id"])}

    @app.post('/api/discord/link')
    def discord_link(user=Depends(current)):
        code=community.hub.new_code(user['id'])
        store.audit(user['id'],'discord_link_requested')
        return {'command':'/koppel '+code,'expires_in':600}

    @app.post("/api/preferences")
    def preferences(body:PreferencesRequest,user=Depends(current)):
        return store.preferences(user["id"],body.model_dump())

    @app.get('/api/payment-card')
    def card_summary(user=Depends(current)):
        return {'enabled':cards.key is not None,'card':cards.summary(user['id']),'cards':cards.list(user['id'])}

    @app.post('/api/payment-card')
    def card_save(body:CardRequest,user=Depends(current)):
        return {'card':cards.save(user['id'],holder=body.holder.get_secret_value(),number=body.number.get_secret_value(),month=body.month,year=body.year,label=body.label,primary=body.primary)}

    @app.post('/api/payment-card/{card_id}/primary')
    def primary_card(card_id:str,user=Depends(current)):
        cards.set_primary(user['id'],card_id)
        return {'ok':True}

    @app.delete('/api/payment-card/{card_id}')
    def remove_card(card_id:str,user=Depends(current)):
        cards.delete(user['id'],card_id)
        return {'ok':True}

    @app.delete('/api/payment-card')
    def card_delete(user=Depends(current)):
        cards.delete(user['id'])
        return {'ok':True}

    @app.get('/api/community')
    def online(user=Depends(current)):
        with store.conn() as c:
            refreshing=bool(c.execute("SELECT 1 FROM jobs WHERE kind='bookings' AND dedup LIKE 'online-refresh:%' AND status IN ('queued','running') LIMIT 1").fetchone())
        return {'matches':community.list(user['id'])+drafts.list(user['id']),'refreshing':refreshing}

    @app.post('/api/drafts')
    def create_draft(body:DraftRequest,user=Depends(current)):
        from datetime import datetime
        try:
            start,end=datetime.fromisoformat(body.target.replace('Z','+00:00')),datetime.fromisoformat(body.end.replace('Z','+00:00'))
            if start.tzinfo is None or end.tzinfo is None:raise ValueError()
        except ValueError:
            raise ValueError('Kies een geldig tijdslot met tijdzone.') from None
        if not Path(tenant_env(store,user)['PADELBOT_ANDROID_PROFILE_FILE']).exists():raise ValueError('Koppel eerst je eigen KUL-authenticator.')
        draft=drafts.create(user['id'],body.product,body.court,start.isoformat(),end.isoformat())
        try:return enqueue(user,'draft_refresh',{'draft_id':draft['id']})
        except ValueError:
            drafts.abort_pending(draft['id'])
            raise

    @app.get('/api/drafts/mine')
    def own_drafts(user=Depends(current)):
        return {'drafts':drafts.own_status(user['id'])}

    @app.post('/api/drafts/{draft_id}/cancel')
    def cancel_draft(draft_id:str,user=Depends(current)):
        drafts.stop(draft_id,user['id'])
        return enqueue(user,'draft_release',{'draft_id':draft_id})

    @app.post('/api/drafts/{draft_id}/quote')
    def quote_draft(draft_id:str,user=Depends(current)):
        draft=drafts.owned(draft_id,user['id'])
        return enqueue(user,'quote',{'target':draft['target'],'product':draft['product'],'court':draft['court'],'amount':1,'draft_id':draft_id})

    @app.post('/api/drafts/{draft_id}/confirm')
    def confirm_draft(draft_id:str,body:DraftConfirmRequest,user=Depends(current)):
        draft=drafts.owned(draft_id,user['id'])
        return enqueue(user,'book',{'target':draft['target'],'product':draft['product'],'court':draft['court'],'amount':body.amount,'draft_id':draft_id},
                       dedup='draft-confirm:'+draft_id)

    @app.get('/api/community/mine')
    def online_mine(user=Depends(current)):
        return {'shared':community.own_shared(user['id'])}

    @app.post('/api/community/share')
    def share_match(body:ShareRequest,user=Depends(current)):
        community.share(user['id'],body.booking_id,body.shared)
        return {'ok':True}

    @app.post('/api/community/{match_id}/join')
    def join_match(match_id:str,user=Depends(current)):
        match=community.matches.get(match_id)
        if match and match.status=='draft':
            drafts.joined(match_id,user['id'])
            return {'joined':True,'message':'Je neemt deel aan dit onbetaalde voorstel.'}
        community.host(match_id)
        return enqueue(user,'community_join',{'match_id':match_id,'participant_id':user['id']})

    @app.post('/api/community/{match_id}/leave')
    def leave_match(match_id:str,user=Depends(current)):
        community.leave(match_id,user['id'])
        return {'ok':True}

    @app.post('/api/community/refresh')
    def refresh_matches(user=Depends(current)):
        hosts=set(community.hosts_to_refresh())
        if Path(tenant_env(store,user)['PADELBOT_ANDROID_PROFILE_FILE']).exists():
            hosts.add(user['id'])
        for host in sorted(hosts):
            try:
                store.enqueue(host,'bookings',{},dedup=f'online-refresh:{host}:{int(time.time())//120}')
            except ValueError:
                pass
        return {'ok':True}

    @app.get("/api/rules")
    def rules(user=Depends(current)):
        return {"rules":[asdict(r) for r in rules_for(store,user).list_rules()]}

    @app.post("/api/rules")
    def add_rule(body:RuleRequest,user=Depends(current)):
        db = rules_for(store,user)
        if len(db.list_rules())>=10:
            raise ValueError("Je kunt maximaal tien wekelijkse regels instellen.")
        return asdict(db.add_rule(**body.model_dump()))

    @app.post("/api/rules/{rule_id}/toggle")
    def toggle_rule(rule_id:int,user=Depends(current)):
        db = rules_for(store,user)
        rule = db.get_rule(rule_id)
        if not rule:
            raise HTTPException(404,"Regel niet gevonden.")
        db.set_enabled(rule_id,not rule.enabled)
        return {"ok":True}

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id:int,user=Depends(current)):
        if not rules_for(store,user).remove_rule(rule_id):
            raise HTTPException(404,"Regel niet gevonden.")
        return {"ok":True}

    def enqueue(user,kind,payload,dedup=None):
        return {"job_id":platform.enqueue(user,kind,payload) if kind in {'slots','book','quote','login','account','bookings'} else store.enqueue(user['id'],kind,payload,dedup=dedup)}

    @app.post("/api/slots")
    def slots(body:RangeRequest,user=Depends(current)):
        if body.end<body.start:
            raise ValueError("De einddag moet op of na de begindag liggen.")
        return enqueue(user,"slots",body.model_dump())

    @app.post("/api/book")
    def book(body:BookingRequest,user=Depends(current)):
        return enqueue(user,"book",body.model_dump(),f"manual:{user['id']}:{body.product}:{body.court}:{body.target}")

    @app.post("/api/quote")
    def quote(body:BookingRequest,user=Depends(current)):
        return enqueue(user,"quote",body.model_dump())

    @app.post("/api/actions/{kind}")
    def action(kind:str,user=Depends(current)):
        if kind not in {"login","account","bookings"}:
            raise HTTPException(404,"Onbekende taak.")
        return enqueue(user,kind,{})

    @app.get("/api/jobs/{job_id}")
    def job(job_id:str,user=Depends(current)):
        job = store.job(user["id"],job_id)
        if job is None:
            raise HTTPException(404,"Taak niet gevonden.")
        return job

    @app.get("/api/history")
    def history(user=Depends(current)):
        with store.conn() as c:
            rows = c.execute("SELECT id,kind,status,created,result FROM jobs WHERE user_id=? ORDER BY created DESC LIMIT 30",(user["id"],)).fetchall()
        return {"jobs":[{**dict(r),"result":json.loads(r["result"]) if r["result"] else None} for r in rows]}

    @app.post("/api/device/start")
    async def device_start(user=Depends(current)):
        return await devices.begin(user)

    @app.get('/api/device/status')
    async def device_status(user=Depends(current)):
        return await devices.status(user['id'])

    @app.post("/api/device/control")
    async def device_control(body:DeviceRequest,user=Depends(current)):
        if body.action=="stop":
            if devices.enrollment_owner!=user['id'] and devices.user_id!=user["id"]:
                raise HTTPException(404,"Geen eigen toestelsessie.")
            await devices.close()
            return {"ok":True}
        result = await devices.command(user["id"],body.model_dump())
        if result.get("saved"):
            result.update(enqueue(user,"login",{}))
        return result

    @app.post("/api/device/qr")
    async def device_qr(body:QRRequest,user=Depends(current)):
        from .web_device import decode_registration_qr
        if devices.user_id!=user["id"]:
            raise ValueError("Start eerst je eigen toestel.")
        link=await asyncio.to_thread(decode_registration_qr,body.image)
        return await devices.command(user["id"],{"action":"link","link":link})

    @app.post("/api/admin/access")
    def admin_access(request:Request,user=Depends(current)):
        if not user["owner"]:
            raise HTTPException(403,"Geen beheerder.")
        ticket = store.admin_ticket(request.cookies["padel_session"])
        return {"url":config.tail_url+"/admin/verify?"+urlencode({"ticket":ticket})}

    @app.get("/admin/verify")
    def verify_admin(request:Request,ticket:str=""):
        secret = request.headers.get("x-padel-tail-secret","")
        peer = request.client.host if request.client else ""
        identity_ok=request.headers.get("tailscale-user-login","").lower()==config.owner_email.lower()
        allowed_nodes={node.strip() for node in config.tail_admin_node_ids.split(',') if node.strip()}
        identity_ok=identity_ok or request.headers.get('x-padel-tail-node','') in allowed_nodes
        if (not config.tail_proxy_secret or peer!=config.tail_proxy_ip
                or not hmac.compare_digest(secret,config.tail_proxy_secret)
                or not identity_ok):
            raise HTTPException(403,"Deze bevestiging moet via jouw Tailscale-account verlopen.")
        if not store.verify_admin_ticket(ticket,config.owner_email):
            raise HTTPException(403,"De beheerbevestiging is verlopen. Begin opnieuw vanuit je profiel.")
        return RedirectResponse(config.public_url+"/admin/callback?"+urlencode({"ticket":ticket}),status_code=303)

    @app.get("/admin/callback")
    def admin_callback(request:Request,ticket:str="",user=Depends(current)):
        if not user["owner"] or not store.activate_admin(ticket,request.cookies.get("padel_session","")):
            raise HTTPException(403,"Beheerbevestiging ongeldig.")
        store.audit(user["id"],"tailscale_admin")
        return RedirectResponse(config.public_url+"/#admin",status_code=303)

    @app.get("/api/admin/users")
    def users(user=Depends(admin)):
        with store.conn() as c:
            rows = [dict(r) for r in c.execute("SELECT id,email,name,owner,suspended,created FROM users ORDER BY created")]
        for row in rows:
            env = tenant_env(store,row)
            row["connected"] = Path(env["PADELBOT_TOKEN_FILE"]).exists()
            row["rules"] = len(rules_for(store,row).list_rules())
        return {"users":rows,"device_active":devices.user_id is not None}

    @app.post("/api/admin/users/{user_id}/toggle")
    async def suspend(user_id:str,user=Depends(admin)):
        target = store.user(user_id)
        if not target or target["owner"]:
            raise ValueError("Dit profiel kan niet gepauzeerd worden.")
        store.suspend(user["id"],user_id,not target["suspended"])
        community.hub.register(store.user(user_id))
        if not target["suspended"] and devices.user_id==user_id:
            await devices.close()
        return {"ok":True}

    app.mount("/assets",StaticFiles(directory=static),name="assets")

    @app.get("/")
    def index():
        return FileResponse(static/"index.html")

    return app


def main():
    import uvicorn
    uvicorn.run(create_app(),host="0.0.0.0",port=8090,proxy_headers=False,access_log=False)


if __name__=="__main__":
    main()
