'use strict';
const $ = id => document.getElementById(id);
let me = null, activePage = 'slots', booking = null, deviceTimer = null, communityFilter = 'all';
let slotsCache=null,slotsLoading=null,slotsCacheDay=null,invitedMatch=null,inviteScrolled=false,inviteRefreshTried=false;
const days = ['Maandag','Dinsdag','Woensdag','Donderdag','Vrijdag','Zaterdag','Zondag'];
const titles={dashboard:'Dashboard',online:'Matches',admin:'Beheer'};
const date = iso => new Date(iso).toLocaleDateString('nl-BE',{timeZone:'Europe/Brussels',weekday:'long',day:'numeric',month:'long'});
const clock = iso => new Date(iso).toLocaleTimeString('nl-BE',{timeZone:'Europe/Brussels',hour:'2-digit',minute:'2-digit'});
const euro = amount => Number(amount).toLocaleString('nl-BE',{style:'currency',currency:'EUR'});
const el = (tag,cls,text) => {const x=document.createElement(tag);if(cls)x.className=cls;if(text!==undefined)x.textContent=text;return x;};
if(!$('pair-check-login')){const button=el('button','button light','Controle vernieuwen');button.id='pair-check-login';button.type='button';button.hidden=true;$('pair-complete').append(button);}
function message(text,error=false,busy=false){$('status').textContent=text;$('status').className='status'+(error?' error':'')+(busy?' busy':'');$('status').hidden=!text;}
async function api(path,method='GET',body,timeoutMs=30000){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeoutMs);try{const r=await fetch(path,{method,signal:controller.signal,credentials:'same-origin',headers:{'Content-Type':'application/json',...(me?{'X-CSRF-Token':me.csrf}:{})},...(body===undefined?{}:{body:JSON.stringify(body)})});let data;try{data=await r.json();}catch(error){if(controller.signal.aborted)throw error;throw new Error('De server gaf geen geldig antwoord. Probeer opnieuw.');}if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'Controleer je invoer en probeer opnieuw.');if(data.error)throw new Error(data.error);return data;}catch(error){if(controller.signal.aborted)throw new Error('De aanvraag duurt te lang. Controleer de voortgang en probeer opnieuw.');throw error;}finally{clearTimeout(timer);}}
async function guarded(fn){try{await fn();}catch(e){if($('device-dialog').open)$('device-message').textContent=e.message;else message(e.message,true);}}
async function waitForJob(job_id,text='KUL controleren...',timeoutMs=740000){message(text,false,true);const deadline=Date.now()+timeoutMs;for(let i=0;Date.now()<deadline;i++){await new Promise(r=>setTimeout(r,Math.min(2000,Math.max(1,deadline-Date.now()))));if(Date.now()>=deadline)break;const job=await api('/api/jobs/'+job_id,'GET',undefined,Math.min(30000,Math.max(1,deadline-Date.now())));if(job.status==='done'){message('');return job.result;}if(['failed','unknown','cancelled'].includes(job.status))throw new Error(job.result?.error||'De taak kon niet worden afgerond.');if(i===8)message('Nog even geduld. De KUL-login wordt gecontroleerd.',false,true);}const error=new Error('De controle loopt nog. Je kunt de voortgang opnieuw ophalen.');error.pending=true;throw error;}
async function task(path,body={},text='Even ophalen bij KU Leuven Sport...'){message(text,false,true);const queued=await api(path,'POST',body);if(!queued.job_id){message('');return queued;}return waitForJob(queued.job_id,text);}
function empty(container,title,text){container.replaceChildren();const box=el('div','empty');box.append(el('span','empty-icon','◫'),el('h3','',title),el('p','',text));container.append(box);}
function applyMe(){
 $('profile-name').textContent=me.name.split(' ')[0];$('avatar').textContent=me.name.charAt(0).toUpperCase();$('admin-nav').hidden=!(me.owner&&me.admin_eligible);$('admin-access').hidden=me.owner!==true;
 $('device-state').textContent=me.needs_attention?'Controle nodig':me.device_ready?'Gekoppeld':'Niet gekoppeld';$('start-device').textContent=me.device_ready?'Opnieuw koppelen':'KUL koppelen';
 document.querySelector('[data-open-setting="kul"]').textContent=me.connected?'Beheren':'Koppelen';
 $('setup-kul-state').textContent=me.needs_attention?'Controle nodig':me.connected?'Gekoppeld':me.device_ready?'Controle nodig':'Niet gekoppeld';
 $('setup-discord-state').textContent=me.discord_linked?'Gekoppeld':'Niet gekoppeld';$('discord-status').textContent=me.discord_linked?'Gekoppeld':'Niet gekoppeld';
 $('add-discord').textContent=me.discord_linked?'Bot toevoegen \u2197':'Toevoegen \u2197';
 $('discord-fallback').hidden=!!me.discord_oauth||!!me.discord_linked;$('link-discord').hidden=!!me.discord_linked;
 if(me.discord_linked)$('discord-link-code').hidden=true;
 $('auto-pay').checked=!!me.preferences.automatic_payment;$('max-amount').value=me.preferences.max_amount||0;
 $('payment-mode').value=me.preferences.payment_mode||'discord';
 $('setup-payment-state').textContent=me.preferences.payment_mode==='discord'?'Betaallink':me.preferences.payment_mode==='kul'?'Creditcard bij KUL':'Kies je betaalwijze';
 const p=me.payment,box=$('payment-state');box.replaceChildren();
 if(p)box.append(el('p','help',p.stored_card?'Creditcard gekoppeld':('KUL-saldo '+euro(p.balance))));
 for(const item of me.deliveries||[])if(['failed','unknown'].includes(item.status))box.append(el('p','pair-message',item.error));
}
async function loadMe(){me=await api('/api/me');applyMe();}
async function showPage(page){
 const invite=page.match(/^match\/([a-f0-9]{32})$/);if(invite){invitedMatch=invite[1];inviteScrolled=false;inviteRefreshTried=false;communityFilter='all';page='online';}else if(page!=='online')invitedMatch=null;
 const panel={settings:'profile-settings',rules:'autobook-panel',bookings:'my-bookings-panel'}[page];
 if(panel)page='dashboard';if(page==='slots')page='dashboard';
 if(!titles[page]||(page==='admin'&&!(me?.owner&&me.admin_eligible)))page='dashboard';activePage=page;
 for(const section of document.querySelectorAll('.page'))section.hidden=section.id!=='page-'+page;
 for(const button of document.querySelectorAll('nav [data-page]'))button.classList.toggle('active',button.dataset.page===page);
 $('page-title').textContent=titles[page];history.replaceState(null,'',page==='online'&&invitedMatch?'#match/'+invitedMatch:'#'+page);message('');
 if(panel){$(panel).open=true;$(panel).scrollIntoView({block:'start',behavior:'smooth'});}
 if(page==='online')await loadOnline();if(page==='admin')await loadAdmin();
 if(page==='dashboard'){void guarded(loadPayments);if(me.connected&&(!slotsCache||slotsCacheDay!==brusselsDay())&&!slotsLoading)void guarded(()=>slots());}
 if(panel==='profile-settings')await Promise.all([activity(),loadCard()]);if(panel==='autobook-panel')await loadRules();
 if(panel==='my-bookings-panel'&&!$('bookings').children.length)await bookings();
}
function brusselsDay(iso=new Date().toISOString()){
 const parts=new Intl.DateTimeFormat('en',{timeZone:'Europe/Brussels',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date(iso));
 const value=key=>Number(parts.find(part=>part.type===key).value);
 return Date.UTC(value('year'),value('month')-1,value('day'));
}
function renderSlots(){
 const start=Number($('range-start').value),end=Number($('range-end').value);
 if(!Number.isInteger(start)||!Number.isInteger(end)||start<0||end>6||end<start)throw new Error('Kies dagen tussen 0 en 6.');
 document.querySelectorAll('[data-range]').forEach(button=>button.classList.toggle('selected',button.dataset.range===start+','+end));
 const today=brusselsDay(),groups=new Map(),filtered=(slotsCache||[]).filter(slot=>{const day=Math.round((brusselsDay(slot.start)-today)/86400000);return day>=start&&day<=end&&new Date(slot.start).getTime()>Date.now();});
 for(const slot of filtered){const key=date(slot.start);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(slot);}
 $('slots').replaceChildren();$('slot-summary').textContent=filtered.length+' vrije momenten';
 if(!groups.size){empty($('slots'),'Geen vrije momenten','Kies een andere dag.');return;}
 for(const [key,items] of groups){const row=el('div','slot-day'),label=el('div','slot-date',key.split(' ')[0]);label.append(el('strong','',key.split(' ').slice(1).join(' ')));const options=el('div','slot-options');for(const slot of items){const button=el('button','slot');button.append(el('strong','',clock(slot.start)+' - '+clock(slot.end)),el('small','',(slot.name||'Padel')+' \u00b7 Bekijk prijs'));button.addEventListener('click',()=>guarded(()=>quote(slot)));options.append(button);}row.append(label,options);$('slots').append(row);}
}
async function slots(refresh=false){
 if(!me.connected){$('slot-summary').textContent='';$('slots').replaceChildren(el('p','empty-line','Koppel KUL om vrije momenten te zien.'));return;}
 if(slotsCacheDay!==brusselsDay())slotsCache=null;
 if(slotsLoading){await slotsLoading;renderSlots();return;}
 if(!slotsCache||refresh){$('refresh-slots').disabled=true;$('refresh-slots').setAttribute('aria-busy','true');if(!slotsCache)$('slots').replaceChildren(el('p','empty-line','7 dagen ophalen...'));
  slotsLoading=(async()=>{const data=await task('/api/slots',{start:0,end:6},'Vrije momenten voor 7 dagen ophalen...');slotsCache=data.slots||[];slotsCacheDay=brusselsDay();})();
  try{await slotsLoading;}finally{slotsLoading=null;$('refresh-slots').disabled=false;$('refresh-slots').setAttribute('aria-busy','false');}
 }
 renderSlots();
}
function safePaymentUrl(raw){try{const url=new URL(raw);return url.protocol==='https:'&&!url.username&&!url.password&&!url.port&&!url.hash&&((url.hostname==='payment.direct.worldline-solutions.com')||(url.hostname==='www.kuleuven.be'&&url.pathname==='/sapredir/kredietbetaling_Detail.html'))?url.href:null;}catch{return null;}}
async function loadPayments(){
 const data=await api('/api/payments'),container=$('payment-links');container.replaceChildren();
 for(const payment of data.payments||[]){const url=safePaymentUrl(payment.url);if(!url||payment.status!=='pending')continue;const row=el('div','payment-item'),info=el('div');info.append(el('strong','',date(payment.target)+' \u00b7 '+clock(payment.target)),el('small','',euro(payment.amount)));const link=el('a','button dark','Betalen');link.href=url;link.target='_blank';link.rel='noopener noreferrer';row.append(info,link);container.append(row);}
 $('pending-payments').hidden=!container.children.length;
}
async function quote(slot){const result=await task('/api/quote',{target:slot.start,product:slot.product,court:slot.court,amount:1},'Jouw persoonlijke prijs controleren…');booking=result;$('create-draft').hidden=false;const detail=$('booking-detail');detail.replaceChildren(el('p','',(slot.name||'Padel')+' · '+date(result.target)+' · '+clock(result.target)),el('div','booking-amount',euro(result.amount)));$('confirm-book').textContent='Boeken en betalen · '+euro(result.amount);$('booking-payment-note').textContent=me.preferences.payment_mode==='discord'?'Eerst KUL-saldo, anders een betaallink.':'Met KUL-saldo of je gekoppelde creditcard.';$('confirm-book').textContent=(me.preferences.payment_mode==='discord'?'Boeken':'Boeken en betalen')+' - '+euro(result.amount);$('booking-dialog').showModal();}
async function confirmBook(){if(!booking)return;$('confirm-book').disabled=true;try{$('booking-dialog').close();const result=booking.draft_id?await task('/api/drafts/'+booking.draft_id+'/confirm',{amount:Number(booking.amount)},'Boeking en betaling bevestigen...'):await task('/api/book',{target:booking.target,product:booking.product,court:booking.court,amount:Number(booking.amount)},'Je boeking wordt uitgevoerd...');message(result.message);booking=null;await loadMe();await loadPayments();if(result.payment_available)$('pending-payments').scrollIntoView({block:'center',behavior:'smooth'});}finally{$('confirm-book').disabled=false;}}
async function createDraft(){if(!booking||booking.draft_id)return;$('create-draft').disabled=true;try{$('booking-dialog').close();await task('/api/drafts',{product:booking.product,court:booking.court,target:booking.target,end:booking.end},'Onbetaald voorstel aanmaken...');booking=null;communityFilter='draft';await showPage('online');}finally{$('create-draft').disabled=false;}}
async function draftQuote(match){const result=await task('/api/drafts/'+match.id+'/quote',{},'Actuele prijs controleren...');booking={...result,draft_id:match.id};$('create-draft').hidden=true;const detail=$('booking-detail');detail.replaceChildren(el('p','',match.title+' - '+date(result.target)+' - '+clock(result.target)),el('div','booking-amount',euro(result.amount)));$('confirm-book').textContent='Boeken en betalen - '+euro(result.amount);$('booking-payment-note').textContent=me.preferences.payment_mode==='discord'?'Eerst KUL-saldo, anders een betaallink.':'Met KUL-saldo of je gekoppelde creditcard.';$('confirm-book').textContent=(me.preferences.payment_mode==='discord'?'Boeken':'Boeken en betalen')+' - '+euro(result.amount);$('booking-dialog').showModal();}

async function shareMatch(id,url){
 if(!/^[a-f0-9]{32}$/.test(id))throw new Error('Deze match kan niet worden gedeeld.');
 $('share-url').value=location.origin+'/#match/'+id;$('native-share').hidden=!navigator.share;$('share-feedback').textContent='';
 $('share-dialog').showModal();
}
async function bookings(){
 const data=await task('/api/actions/bookings'),sharing=(await api('/api/community/mine')).shared;
 if(!data.bookings.length){empty($('bookings'),'Geen komende boekingen','');return;}
 $('bookings').replaceChildren();
 for(const b of data.bookings){const row=el('div','list-item'),text=el('div');text.append(el('h3','',date(b.start)),el('p','',clock(b.start)+' - '+clock(b.end)),el('small','',b.name||b.description));row.append(text);const actions=el('div','actions');actions.append(el('span','badge',b.confirmed?'Bevestigd':'Niet bevestigd'));
  if(b.confirmed){let shared=!!sharing[b.id];const toggle=el('button','button light',shared?'Offline zetten':'Online zetten'),share=el('button','button light','Delen');toggle.addEventListener('click',()=>guarded(async()=>{toggle.disabled=true;try{await api('/api/community/share','POST',{booking_id:b.id,shared:!shared});shared=!shared;toggle.textContent=shared?'Offline zetten':'Online zetten';}finally{toggle.disabled=false;}}));share.addEventListener('click',()=>guarded(async()=>{share.disabled=true;try{const match=await api('/api/community/share','POST',{booking_id:b.id,shared:true});shared=true;toggle.textContent='Offline zetten';await shareMatch(match.id,match.url);}finally{share.disabled=false;}}));actions.append(toggle,share);}
  row.append(actions);$('bookings').append(row);
 }
}
async function loadOnline(){const data=await api('/api/community');const container=$('online-matches');container.replaceChildren();if(invitedMatch&&!data.matches.some(match=>match.id===invitedMatch)&&!inviteRefreshTried){inviteRefreshTried=true;const refresh=await api('/api/community/'+invitedMatch+'/refresh','POST',{});if(refresh.refreshing){container.append(el('p','empty-line','Match controleren...'));message('Match controleren...',false,true);return true;}}document.querySelectorAll('[data-community-filter]').forEach(button=>button.classList.toggle('selected',button.dataset.communityFilter===communityFilter));const matches=data.matches.filter(match=>communityFilter==='all'||match.kind===communityFilter);if(!matches.length){if(invitedMatch&&data.refreshing){container.append(el('p','empty-line','Match controleren...'));return data.refreshing;}empty(container,communityFilter==='draft'?'Geen open drafts':'Geen open matches','Maak een draft vanuit Beschikbaarheid of deel een bevestigde reservatie via Mijn boekingen.');if(communityFilter!=='confirmed')await loadOwnDraftIssues(container,data.matches);return data.refreshing;}for(const match of matches){const row=el('div','list-item match-card');row.dataset.matchId=match.id;if(match.id===invitedMatch)row.classList.add('invited-match');const info=el('div');const head=el('div','match-card-head');head.append(el('h3','',match.title),el('span','player-count',match.count+'/'+match.capacity));info.append(head,el('span','badge '+(match.kind==='draft'?'draft-badge':''),match.kind==='draft'?'Voorstel':'Bevestigd'),el('p','match-date',date(match.start)+' - '+clock(match.start)+' tot '+clock(match.end)),el('p','',match.players.join(', ')));if(match.kind==='draft')info.append(el('small','','Verloopt '+date(new Date(match.expires*1000).toISOString())+' om '+clock(new Date(match.expires*1000).toISOString())));else info.append(el('small','','Gecontroleerd om '+clock(new Date(match.verified*1000).toISOString())+(match.origin==='discord'?' - Ook op Discord':'')));row.append(info);const actions=el('div','match-actions');if(match.organiser){if(match.kind==='draft'){const pay=el('button','button dark','Boeken en betalen');pay.addEventListener('click',()=>guarded(()=>draftQuote(match)));const stop=el('button','button light','Stoppen');stop.addEventListener('click',()=>guarded(async()=>{await task('/api/drafts/'+match.id+'/cancel',{},'Eigen draft-item opruimen...');await loadOnline();}));actions.append(pay,stop);}else actions.append(el('span','badge','Organisator'));}else{const button=el('button','button '+(match.joined?'light':'dark'),match.joined?'Uitschrijven':'Deelnemen');button.addEventListener('click',()=>guarded(async()=>{button.disabled=true;try{if(match.joined)await api('/api/community/'+match.id+'/leave','POST',{});else await task('/api/community/'+match.id+'/join',{},match.kind==='draft'?'Aanmelden voor voorstel...':'Reservering bij KUL controleren...');await loadOnline();}finally{button.disabled=false;}}));actions.append(button);}const share=el('button','button light','Delen');share.addEventListener('click',()=>guarded(()=>shareMatch(match.id)));actions.append(share);row.append(actions);container.append(row);}if(invitedMatch&&!inviteScrolled){const invited=Array.from(container.children).find(row=>row.dataset.matchId===invitedMatch);if(invited){invited.scrollIntoView({block:'center',behavior:'smooth'});inviteScrolled=true;message('');}else message('Deze match is niet meer beschikbaar.');}if(communityFilter!=='confirmed')await loadOwnDraftIssues(container,data.matches);return data.refreshing;}

async function loadOwnDraftIssues(container,visible){const data=await api('/api/drafts/mine');const shown=new Set(visible.map(match=>match.id));const states={pending:'Draft wordt aangemaakt',uncertain:'Controle nodig',stopping:'Draft wordt opgeruimd',cleanup_failed:'Controleer je KUL-winkelwagen',payment_pending:'Controleer je KUL-boeking en betaling',converting:'Boeking wordt verwerkt',cancelled:'Draft gestopt',expired:'Draft verlopen',unavailable:'Terrein niet meer beschikbaar'};for(const draft of data.drafts){if(shown.has(draft.id)||['cancelled','expired','unavailable'].includes(draft.state))continue;const row=el('div','list-item');const info=el('div');info.append(el('h3','',draft.title),el('p','',date(draft.start)+' - '+clock(draft.start)),el('span','badge',states[draft.state]||'Controle nodig'));if(draft.error)info.append(el('p','help',draft.error));row.append(info);if(['uncertain','cleanup_failed'].includes(draft.state)){const stop=el('button','button light','Stoppen');stop.addEventListener('click',()=>guarded(async()=>{await task('/api/drafts/'+draft.id+'/cancel',{},'Eigen draft-item opruimen...');await loadOnline();}));row.append(stop);}else if(['payment_pending','converting'].includes(draft.state)){const link=el('a','button light','Controleer bij KUL');link.href='https://usc.kuleuven.cloud/';link.target='_blank';link.rel='noopener';row.append(link);}container.append(row);}}

async function refreshOnline(){await api('/api/community/refresh','POST',{});message('Reserveringen bij KUL controleren...',false,true);for(let i=0;i<65;i++){await new Promise(r=>setTimeout(r,2000));if(activePage!=='online')return;const pending=await loadOnline();if(!pending){message('');return;}}message('De controle loopt nog. Vernieuw over een ogenblik.');}
async function loadCard(){const data=await api('/api/payment-card');const container=$('saved-card');container.replaceChildren();for(const input of $('card-form').querySelectorAll('input,select,button[type=submit]'))input.disabled=!data.enabled;if(!data.enabled){container.append(el('p','help','Kaartopslag is nog niet ingesteld.'));return;}if(!data.cards.length)return;for(const card of data.cards){const row=el('div','wallet-card');const info=el('div');info.append(el('h4','',card.label),el('p','',card.brand+' '+String.fromCharCode(8226).repeat(4)+' '+card.last4+' - '+String(card.month).padStart(2,'0')+'/'+card.year));row.append(info);const actions=el('div','actions');if(card.primary)actions.append(el('span','badge','Standaard'));else{const primary=el('button','button light','Als standaard');primary.type='button';primary.addEventListener('click',()=>guarded(async()=>{await api('/api/payment-card/'+card.id+'/primary','POST',{});await loadCard();}));actions.append(primary);}const remove=el('button','text-button danger-link','Verwijderen');remove.type='button';remove.addEventListener('click',()=>guarded(async()=>{await api('/api/payment-card/'+card.id,'DELETE',{});await loadCard();}));actions.append(remove);row.append(actions);container.append(row);}}

async function loadRules(){const data=await api('/api/rules');if(!data.rules.length){empty($('rules'),'Geen wekelijkse regels','Voeg een wekelijks speelmoment toe.');return;}$('rules').replaceChildren();for(const rule of data.rules){const row=el('div','list-item');const text=el('div');text.append(el('h3','',days[rule.day_of_week]),el('p','',rule.time_local+(rule.end_time_local&&rule.end_time_local!==rule.time_local?' – '+rule.end_time_local:'')),el('small','',rule.notes||'Wekelijks'));const actions=el('div','actions');const toggle=el('button','text-button',rule.enabled?'Pauzeren':'Activeren');toggle.addEventListener('click',()=>guarded(async()=>{await api('/api/rules/'+rule.id+'/toggle','POST',{});await loadRules();}));const remove=el('button','text-button danger-link','Verwijderen');remove.addEventListener('click',()=>guarded(async()=>{await api('/api/rules/'+rule.id,'DELETE');await loadRules();}));actions.append(toggle,remove);row.append(text,actions);$('rules').append(row);}}
async function activity(){const data=await api('/api/history');$('activity').replaceChildren();const names={slots:'Beschikbaarheid ophalen',login:'KUL-account koppelen',account:'Account controleren',bookings:'Boekingen ophalen',rule:'Automatische boeking',book:'Boeking uitvoeren',quote:'Prijs controleren',community_join:'Deelnemen aan match',draft_refresh:'Draft controleren',draft_release:'Draft opruimen'};const states={queued:'In de wachtrij',running:'Bezig',done:'Klaar',failed:'Aandacht nodig',unknown:'Controleer bij KUL',cancelled:'Geannuleerd'};if(!data.jobs.length){$('activity').append(el('p','help','Nog geen activiteit.'));return;}for(const j of data.jobs){const row=el('div','activity-row');const info=el('div','',names[j.kind]||j.kind);if(j.result?.error)info.append(el('small','',j.result.error));else if(j.result?.message)info.append(el('small','',j.result.message));row.append(info,el('span','',states[j.status]||j.status));$('activity').append(row);}}
async function checkAccount(){await task('/api/actions/'+(me.connected?'account':'login'),{},'KUL controleren...');await loadMe();message('KUL is verbonden.');if(!slotsCache&&!slotsLoading)void guarded(()=>slots());}
async function loadAdmin(){if(!(me?.owner&&me.admin_eligible))return;$('admin-gate').hidden=me.admin;if(!me.admin){$('admin-users').replaceChildren();return;}const data=await api('/api/admin/users');const container=$('admin-users');container.replaceChildren(el('div','section-label',data.users.length+' PROFIELEN · TOESTEL '+(data.device_active?'ACTIEF':'IN RUST')));for(const u of data.users){const row=el('div','list-item');const info=el('div');info.append(el('h3','',u.name),el('p','user-email',u.email),el('small','',(u.connected?'KUL gekoppeld':'Nog niet gekoppeld')+' · '+u.rules+' regels'+(u.owner?' · Beheerder':'')));row.append(info);if(!u.owner){const toggle=el('button','button light',u.suspended?'Hervatten':'Pauzeren');toggle.addEventListener('click',()=>guarded(async()=>{await api('/api/admin/users/'+u.id+'/toggle','POST',{});await loadAdmin();}));row.append(toggle);}container.append(row);}}
let pairPhase='waiting',pairReady=false,pairPolling=false,pairSaving=false,pairSubmitted=false,pairStepNumber=1,pairFinished=false,pendingRegistrationFile=null,registrationSending=false,registrationEpoch=0,registrationPreviewSequence=0,pairStateError=false,pairLoginJob=null;
if(!$('device-pin-confirm')){const label=el('label','','PIN bevestigen'),input=el('input');input.id='device-pin-confirm';input.type='password';input.inputMode='numeric';input.pattern='[0-9]{4}';input.maxLength=4;input.autocomplete='new-password';input.required=true;input.disabled=true;label.append(input);$('finish-device').before(label);}
$('device-advanced')?.remove();$('pin-check-label')?.remove();
const eyeIcon='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>';
for(const id of ['device-pin','device-pin-confirm']){const input=$(id);if(!document.querySelector('[data-pin-toggle="'+id+'"]')){const wrapper=el('div','pin-input'),button=el('button','pin-eye');input.before(wrapper);wrapper.append(input,button);button.type='button';button.dataset.pinToggle=id;}const button=document.querySelector('[data-pin-toggle="'+id+'"]');button.innerHTML=eyeIcon;button.setAttribute('aria-label',id==='device-pin'?'PIN tonen':'PIN bevestiging tonen');button.setAttribute('aria-pressed','false');button.addEventListener('click',()=>{const visible=input.type==='password';input.type=visible?'text':'password';button.setAttribute('aria-pressed',String(visible));button.setAttribute('aria-label',(id==='device-pin'?'PIN':'PIN bevestiging')+(visible?' verbergen':' tonen'));});}
function clearPairPins(){for(const id of ['device-pin','device-pin-confirm']){$(id).value='';$(id).type='password';const button=document.querySelector('[data-pin-toggle="'+id+'"]');button.setAttribute('aria-pressed','false');button.setAttribute('aria-label',(id==='device-pin'?'PIN':'PIN bevestiging')+' tonen');}}
function pairButton(busy=false,label='Koppelen'){const button=$('finish-device');button.disabled=busy||!pairReady||pairFinished;button.textContent=label;button.setAttribute('aria-busy',String(busy));}
function clearRegistrationPreview(){registrationPreviewSequence++;pendingRegistrationFile=null;$('qr-preview').hidden=true;$('qr-preview-image').removeAttribute('src');$('qr-preview-state').textContent='';$('device-qr').value='';}
function pairStep(step){pairStepNumber=step;document.querySelectorAll('[data-pair-step]').forEach(x=>x.hidden=Number(x.dataset.pairStep)!==step);document.querySelectorAll('[data-pair-marker]').forEach(x=>{if(Number(x.dataset.pairMarker)===step)x.setAttribute('aria-current','step');else x.removeAttribute('aria-current');});}
async function deviceCommand(action,extra={}){return api('/api/device/control','POST',{action,...extra},90000);}
async function pollEnrollment(){
 if(pairPolling||pairSaving||pairFinished||!$('device-dialog').open)return;pairPolling=true;const epoch=registrationEpoch;
 try{
  const result=await api('/api/device/status');if(epoch!==registrationEpoch||pairSaving||!$('device-dialog').open)return;pairReady=result.ready===true;
  $('device-qr').disabled=registrationSending||pairSaving;$('upload-qr').disabled=registrationSending||pairSaving;$('paste-qr').disabled=registrationSending||pairSaving;$('send-device-link').disabled=!pairReady||registrationSending||pairSaving;
  if(result.state==='failed')throw new Error(result.error||'Het toestel kon niet starten.');
  if(result.state==='idle')throw new Error('De koppelsessie is afgelopen. Start het toestel opnieuw.');
  $('retry-device').hidden=true;
  if(!pairReady){$('pair-progress').textContent='Toestel starten...';return;}
  if(pendingRegistrationFile&&!registrationSending){try{await flushRegistration();}catch(error){$('device-message').textContent=error.message;}return;}
  pairPhase=result.phase||'waiting';
  const prompts={enroll_intro:'KUL-registratie voorbereiden...',enroll_pin:'Vul de toestel-PIN in.',needs_registration:'Voeg je registratie-QR toe.',waiting:pairSubmitted?'Registratie verstuurd. Bevestig zo nodig op je eigen telefoon; we wachten op KUL.':'Toestel klaar. Volg de stappen om KUL te koppelen.',pin_create:'KUL vraagt om een nieuwe PIN. Je kunt nu afronden.',pin_confirm:'Vul de PIN en bevestiging in.',registered:'Rond de koppeling af.',pin_rejected:'KUL weigert deze PIN. Kies een minder voorspelbare PIN.',login_qr:'Dit is een aanmeld-QR. Gebruik in KUL-accountbeheer Toestel toevoegen.'};
  $('pair-progress').textContent=prompts[pairPhase]||prompts.waiting;
  const canFinish=['pin_create','pin_confirm','enroll_pin','registered'].includes(pairPhase);
  $('device-pin').disabled=$('device-pin-confirm').disabled=!pairSubmitted&&!canFinish;pairButton();
  if(canFinish){pairStep(3);$('pair-pin-title').textContent=pairPhase==='pin_create'?'Kies een PIN':'Toestel-PIN';$('pair-pin-help').textContent='4 cijfers.';if(pairStateError){$('device-message').textContent='';pairStateError=false;}}
  if(pairPhase==='login_qr'){pairStep(2);pairSubmitted=false;}
 }catch(error){if(epoch!==registrationEpoch)return;pairReady=false;pairButton();$('pair-progress').textContent=error.message;$('device-message').textContent=error.message;$('retry-device').hidden=false;clearInterval(deviceTimer);$('retry-device').scrollIntoView({block:'nearest'});}
 finally{if(epoch===registrationEpoch)pairPolling=false;}
}
async function startDevice(){
 pairStateError=false;pairSaving=false;pairPolling=false;pairLoginJob=null;$('pair-check-login').hidden=true;pairButton();
 clearInterval(deviceTimer);registrationEpoch++;clearRegistrationPreview();registrationSending=false;pairFinished=false;$('pair-complete').hidden=true;$('qr-feedback').textContent='';pairReady=false;pairSubmitted=false;pairPhase='waiting';pairStep(1);clearPairPins();$('device-message').textContent='';$('retry-device').hidden=true;$('device-qr').disabled=false;$('upload-qr').disabled=false;$('paste-qr').disabled=false;$('send-device-link').disabled=true;$('device-pin').disabled=$('device-pin-confirm').disabled=true;$('finish-device').disabled=true;
 if(!$('device-dialog').open)$('device-dialog').showModal();
 $('pair-progress').textContent='Toestel starten...';
 const epoch=registrationEpoch;
 try{await api('/api/device/start','POST',{});if(epoch!==registrationEpoch||!$('device-dialog').open)return;await pollEnrollment();deviceTimer=setInterval(pollEnrollment,2000);}catch(error){if(epoch!==registrationEpoch)return;$('device-message').textContent=error.message;$('retry-device').hidden=false;}
}
async function closeDevice(){registrationEpoch++;pairSaving=false;pairPolling=false;pairButton();clearRegistrationPreview();clearInterval(deviceTimer);deviceTimer=null;clearPairPins();$('device-link').value='';$('device-dialog').close();try{await deviceCommand('stop');}catch{}}
function clipboardImage(data){for(const file of Array.from(data?.files||[]))if(file.type.startsWith('image/'))return file;for(const item of Array.from(data?.items||[])){if(item.kind==='file'&&item.type.startsWith('image/')){const file=item.getAsFile();if(file)return file;}}return null;}
async function sendRegistration(file){
 if(pairSaving||registrationSending)throw new Error('De vorige afbeelding wordt nog verwerkt.');
 if(!file.size||file.size>8000000)throw new Error('Gebruik een screenshot van maximaal 8 MB.');
 const epoch=registrationEpoch,sequence=++registrationPreviewSequence;pendingRegistrationFile=null;
 const preview=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('De afbeelding kon niet worden gelezen.'));reader.readAsDataURL(file);});
 if(epoch!==registrationEpoch||sequence!==registrationPreviewSequence||!$('device-dialog').open)return;
 pendingRegistrationFile={encoded:preview.split(',')[1]};pairStep(2);$('qr-preview-image').src=preview;$('qr-preview').hidden=false;$('qr-preview-state').textContent='Afbeelding toegevoegd';$('remove-qr').disabled=false;$('qr-preview').scrollIntoView({block:'nearest',behavior:'smooth'});$('device-message').textContent='';$('qr-feedback').textContent=pairReady?'Afbeelding ontvangen. QR-code zoeken...':'Afbeelding ontvangen. We gaan automatisch verder zodra het toestel klaar is.';
 if(pairReady)await flushRegistration();
}
async function flushRegistration(){
 if(!pairReady||registrationSending||!pendingRegistrationFile||!$('device-dialog').open)return;
 const {encoded}=pendingRegistrationFile,epoch=registrationEpoch;pendingRegistrationFile=null;registrationSending=true;
 $('device-qr').disabled=true;$('upload-qr').disabled=true;$('paste-qr').disabled=true;$('remove-qr').disabled=true;$('qr-preview-state').textContent='QR-code zoeken...';
 try{
  if(epoch!==registrationEpoch||!$('device-dialog').open)return;
  await api('/api/device/qr','POST',{image:encoded});
  if(epoch!==registrationEpoch||!$('device-dialog').open)return;
  $('qr-feedback').textContent='QR-code ontvangen.';$('qr-preview-state').textContent='QR-code gevonden en verstuurd';pairSubmitted=true;$('device-pin').disabled=$('device-pin-confirm').disabled=false;pairButton();$('device-message').textContent='';$('pair-progress').textContent='Registratie verstuurd. We wachten op KUL.';pairStep(3);await pollEnrollment();
 }catch(error){if(epoch!==registrationEpoch)return;$('qr-feedback').textContent='';$('qr-preview-state').textContent='Afbeelding niet verwerkt';throw error;}
 finally{if(epoch===registrationEpoch){registrationSending=false;$('device-qr').value='';$('device-qr').disabled=false;$('upload-qr').disabled=false;$('paste-qr').disabled=false;$('remove-qr').disabled=false;}}
}
async function finishEnrollment(event){
 event.preventDefault();if(pairSaving||pairFinished)return;
 const pin=$('device-pin').value,confirmation=$('device-pin-confirm').value;
 if(!/^[0-9]{4}$/.test(pin)||pin!==confirmation){$('device-message').textContent='Vul tweemaal dezelfde PIN van vier cijfers in.';$('device-pin-confirm').focus();return;}
 pairSaving=true;pairStateError=false;const epoch=registrationEpoch;pairButton(true,'Koppelen...');
 try{
  $('device-message').textContent='';$('pair-progress').textContent='Registratie afronden...';
  let state=await api('/api/device/status');
  const deadline=Date.now()+5000;
  while(state.ready&&['waiting','needs_registration'].includes(state.phase)&&Date.now()<deadline){await new Promise(resolve=>setTimeout(resolve,600));if(epoch!==registrationEpoch)return;state=await api('/api/device/status');}
  if(epoch!==registrationEpoch)return;
  if(!state.ready)throw new Error('De koppelsessie is afgelopen. Start het toestel opnieuw.');
  pairPhase=state.phase||'waiting';
  if(!['pin_create','pin_confirm','enroll_pin','registered'].includes(pairPhase)){
   pairStateError=true;throw new Error(pairPhase==='pin_rejected'?'KUL heeft de PIN niet aanvaard.':'KUL verwerkt de registratie nog. Probeer opnieuw zodra de PIN-stap klaar is.');
  }
  if(pairPhase!=='registered'){await deviceCommand('setup_pin',{pin,confirm_pin:confirmation});if(epoch!==registrationEpoch)return;}
  pairButton(true,'Registratie bewaren...');
  const result=await deviceCommand('finish',{pin});if(!result.saved)throw new Error('KUL heeft de registratie nog niet bevestigd.');
  if(epoch!==registrationEpoch)return;clearInterval(deviceTimer);pairFinished=true;pairLoginJob=result.job_id;document.querySelectorAll('[data-pair-step]').forEach(section=>section.hidden=true);$('pair-complete').hidden=false;
  $('pair-progress').textContent='Gekoppeld';$('pair-complete-title').textContent='Gekoppeld';$('pair-complete-text').textContent='padelreservering-auth';message('Je KUL-account is gekoppeld.');
  void checkPairLogin();
 }catch(error){if(epoch!==registrationEpoch)return;throw error;}
 finally{if(epoch===registrationEpoch){clearPairPins();pairSaving=false;pairButton();if($('device-dialog').open&&!pairFinished)await pollEnrollment();}}
}
async function checkPairLogin(timeoutMs=120000){
 if(!pairLoginJob)return;const epoch=registrationEpoch,button=$('pair-check-login');button.hidden=true;button.disabled=true;
 try{await waitForJob(pairLoginJob,'KUL-login controleren...',timeoutMs);if(epoch!==registrationEpoch)return;$('pair-complete-text').textContent='padelreservering-auth';pairLoginJob=null;}
 catch(error){if(epoch!==registrationEpoch)return;message(error.message,!error.pending);$('pair-complete-text').textContent=error.pending?'De logincontrole loopt nog.':'Toestel bewaard. De logincontrole is mislukt: '+error.message;button.hidden=false;}
 finally{if(epoch===registrationEpoch){button.disabled=false;try{await loadMe();await activity();}catch{}}}
}

document.querySelectorAll('[data-page]').forEach(b=>b.addEventListener('click',()=>guarded(()=>showPage(b.dataset.page))));
document.querySelectorAll('[data-range]').forEach(b=>b.addEventListener('click',()=>guarded(async()=>{const [a,z]=b.dataset.range.split(',');$('range-start').value=a;$('range-end').value=z;document.querySelectorAll('[data-range]').forEach(x=>x.classList.toggle('selected',x===b));await slots();})));
$('refresh-slots').addEventListener('click',()=>guarded(()=>slots(true)));$('apply-range').addEventListener('click',()=>guarded(()=>slots()));$('refresh-payments').addEventListener('click',()=>guarded(loadPayments));$('refresh-bookings').addEventListener('click',()=>guarded(bookings));$('confirm-book').addEventListener('click',()=>guarded(confirmBook));$('test-account').addEventListener('click',()=>guarded(checkAccount));
$('rule-form').addEventListener('submit',e=>{e.preventDefault();guarded(async()=>{await api('/api/rules','POST',{day_of_week:Number($('rule-day').value),time_local:$('rule-start').value,end_time_local:$('rule-end').value,notes:$('rule-notes').value});await loadRules();message('Je wekelijkse speelmoment is bewaard.');});});
$('payment-form').addEventListener('submit',e=>{e.preventDefault();guarded(async()=>{await api('/api/preferences','POST',{automatic_payment:$('auto-pay').checked,max_amount:Number($('max-amount').value),payment_mode:$('payment-mode').value});await loadMe();message('Bewaard.');});});
$('link-discord').addEventListener('click',()=>guarded(async()=>{const result=await api('/api/discord/link','POST',{});$('discord-command').textContent=result.command;$('discord-link-code').hidden=false;}));$('copy-discord-link').addEventListener('click',()=>guarded(async()=>{await navigator.clipboard.writeText($('discord-command').textContent);message('Commando gekopieerd.');}));
document.querySelectorAll('[data-community-filter]').forEach(button=>button.addEventListener('click',()=>guarded(async()=>{communityFilter=button.dataset.communityFilter;await loadOnline();})));$('create-draft').addEventListener('click',()=>guarded(createDraft));
$('refresh-online').addEventListener('click',()=>guarded(refreshOnline));
$('card-expiry').addEventListener('input',()=>{const input=$('card-expiry'),digits=input.value.replace(/\D/g,'').slice(0,6);input.value=digits.length>2?digits.slice(0,2)+'/'+digits.slice(2):digits;});
$('card-form').addEventListener('submit',e=>{e.preventDefault();guarded(async()=>{const expiry=$('card-expiry').value.match(/^(\d{2})\/(\d{2}|\d{4})$/);if(!expiry)throw new Error('Controleer de vervaldatum.');const month=Number(expiry[1]),year=Number(expiry[2])+(expiry[2].length===2?2000:0),button=$('save-card');button.disabled=true;try{await api('/api/payment-card','POST',{holder:$('card-holder').value,number:$('card-number').value,month,year});message('Kaart bewaard.');}finally{$('card-form').reset();button.disabled=false;}await loadCard();});});
$('logout').addEventListener('click',()=>guarded(async()=>{await api('/auth/logout','POST',{});location.href='/';}));for(const id of ['verify-admin','admin-access'])$(id).addEventListener('click',()=>guarded(async()=>{const r=await api('/api/admin/access','POST',{});location.href=r.url;}));
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>$(b.dataset.close).close()));
$('start-device').addEventListener('click',()=>guarded(startDevice));$('retry-device').addEventListener('click',()=>guarded(startDevice));$('close-device').addEventListener('click',closeDevice);$('close-device-footer').addEventListener('click',closeDevice);$('device-dialog').addEventListener('cancel',e=>{e.preventDefault();e.stopPropagation();});$('device-dialog').addEventListener('click',e=>{if(e.target===$('device-dialog')){e.preventDefault();e.stopPropagation();}});
$('pair-next').addEventListener('click',()=>pairStep(2));$('pair-back').addEventListener('click',()=>pairStep(1));$('pair-new-qr').addEventListener('click',()=>pairStep(2));
$('link-form').addEventListener('submit',e=>{e.preventDefault();guarded(async()=>{await deviceCommand('link',{link:$('device-link').value});$('device-link').value='';pairSubmitted=true;$('device-message').textContent='';pairStep(3);await pollEnrollment();});});
$('pin-form').addEventListener('submit',e=>guarded(()=>finishEnrollment(e)));
$('pair-check-login').addEventListener('click',()=>guarded(()=>checkPairLogin()));
$('device-qr').addEventListener('change',()=>guarded(async()=>{const file=$('device-qr').files[0];if(file)await sendRegistration(file);}));
$('upload-qr').addEventListener('click',()=>$('device-qr').click());
$('remove-qr').addEventListener('click',()=>{if(registrationSending||pairSaving)return;clearRegistrationPreview();$('qr-feedback').textContent='';$('device-message').textContent='';});
$('paste-qr').addEventListener('click',()=>{$('paste-qr').focus();$('qr-feedback').textContent='Druk nu Ctrl+V om je screenshot te plakken.';});
document.addEventListener('paste',e=>{if(!$('device-dialog').open||pairSaving||pairFinished)return;const file=clipboardImage(e.clipboardData);if(file){e.preventDefault();guarded(()=>sendRegistration(file));}else if(e.target===$('paste-qr'))$('qr-feedback').textContent='Geen afbeelding op je klembord. Maak eerst een screenshot met Win+Shift+S.';});
function savedInvite(){try{return sessionStorage.getItem('padel-invite');}catch{return null;}}
function rememberInvite(value){try{if(value)sessionStorage.setItem('padel-invite',value);else sessionStorage.removeItem('padel-invite');}catch{}}
(async()=>{try{await loadMe();$('app').hidden=false;$('today').textContent=new Date().toLocaleDateString('nl-BE',{weekday:'short',day:'numeric',month:'long'});const destination=location.hash.slice(1)||savedInvite()||'dashboard';rememberInvite(null);await guarded(()=>showPage(destination));}catch(e){const invite=location.hash.slice(1);if(/^match\/[a-f0-9]{32}$/.test(invite))rememberInvite(invite);$('login').hidden=false;if(!e.message.includes('Meld je aan'))console.warn('Profiel is nog niet beschikbaar.');}})();

setInterval(()=>{if(me&&activePage==='online'&&!document.hidden)guarded(async()=>{await loadOnline();});},15000);

for(const button of document.querySelectorAll('[data-open-setting]'))button.addEventListener('click',()=>guarded(async()=>{await showPage('settings');$(button.dataset.openSetting==='kul'?'kul-settings':'payment-form').scrollIntoView({block:'center',behavior:'smooth'});}));
$('check-payment').addEventListener('click',()=>guarded(checkAccount));
$('profile-settings').addEventListener('toggle',()=>{if($('profile-settings').open)guarded(async()=>{await Promise.all([loadCard(),activity()]);});});
$('autobook-panel').addEventListener('toggle',()=>{if($('autobook-panel').open)guarded(loadRules);});

$('copy-share').addEventListener('click',()=>guarded(async()=>{await navigator.clipboard.writeText($('share-url').value);$('share-feedback').textContent='Link gekopieerd.';}));
$('native-share').addEventListener('click',()=>guarded(async()=>{try{await navigator.share({title:'Padel',text:'Wie doet er mee?',url:$('share-url').value});}catch(error){if(error.name!=='AbortError')throw error;}}));
window.addEventListener('hashchange',()=>{if(me)guarded(()=>showPage(location.hash.slice(1)||'dashboard'));});
let refreshingMe=false;
async function refreshAccess(){if(!me||document.hidden||refreshingMe)return;refreshingMe=true;try{const current=await api('/api/me');me.owner=current.owner;me.admin=current.admin;me.admin_eligible=current.admin_eligible;$('admin-nav').hidden=!(me.owner&&me.admin_eligible);$('admin-access').hidden=me.owner!==true;if(activePage==='admin'&&!me.admin_eligible)await showPage('dashboard');}catch{}finally{refreshingMe=false;}}
window.addEventListener('focus',refreshAccess);document.addEventListener('visibilitychange',()=>{if(!document.hidden)void refreshAccess();});setInterval(refreshAccess,30000);
