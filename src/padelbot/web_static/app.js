'use strict';
const $ = id => document.getElementById(id);
let me = null, activePage = 'slots', booking = null, deviceTimer = null, communityFilter = 'all';
let slotsCache=null,slotsLoading=null,slotsCacheDay=null,availabilityDestination=null,invitedMatch=null,inviteScrolled=false,inviteRefreshTried=false;
let messageTimer=null,accountCheckFlight=null,accountCheckedAt=0;
let cardLoadEpoch=0,cardSaving=false,cardVaultEnabled=true;
let adminMetricsEpoch=0,adminMetricsLoading=false,adminMetricsLoaded=false,adminLoadEpoch=0;
let selectedCancellation=null,bookingsCache=[],bookingSharing={};
const bookingCancellations=new Map();
const days = ['Maandag','Dinsdag','Woensdag','Donderdag','Vrijdag','Zaterdag','Zondag'];
const titles={dashboard:'Dashboard',online:'Matches',admin:'Beheer'};
const date = iso => new Date(iso).toLocaleDateString('nl-BE',{timeZone:'Europe/Brussels',weekday:'long',day:'numeric',month:'long'});
const clock = iso => new Date(iso).toLocaleTimeString('nl-BE',{timeZone:'Europe/Brussels',hour:'2-digit',minute:'2-digit'});
const euro = amount => Number(amount).toLocaleString('nl-BE',{style:'currency',currency:'EUR'});
const el = (tag,cls,text) => {const x=document.createElement(tag);if(cls)x.className=cls;if(text!==undefined)x.textContent=text;return x;};
const personOutline='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="8" r="3.5"/><path d="M5 21v-2a7 7 0 0 1 14 0v2"/></svg>';
function avatarURL(value){try{const url=new URL(value);return url.protocol==='https:'&&!url.username&&!url.password&&!url.port&&!url.hash&&(url.hostname==='cdn.discordapp.com'||url.hostname==='media.discordapp.net'||url.hostname==='googleusercontent.com'||url.hostname.endsWith('.googleusercontent.com'))?url.href:null;}catch{return null;}}
function identityAvatar(identity,guest=false){
 const box=el('span','identity-avatar'+(guest?' guest-avatar':'')),name=String(identity.name||'Speler'),initials=name.trim().split(/\s+/).slice(0,2).map(word=>word.charAt(0)).join('').toUpperCase();box.setAttribute('aria-hidden','true');
 if(guest){const fallback=el('span','guest-outline');fallback.innerHTML=personOutline;box.append(fallback);}else box.append(el('span','avatar-initials',initials||'?'));
 const url=avatarURL(identity.avatar_url||identity.avatar);if(url){const image=el('img');image.src=url;image.alt='';image.loading='lazy';image.decoding='async';image.referrerPolicy='no-referrer';image.addEventListener('error',()=>image.remove(),{once:true});box.append(image);}
 if(guest){box.append(el('span','guest-marker','?'));box.title='Niet geregistreerd';}return box;
}
function playerRoster(match){const roster=el('div','player-roster');for(const person of (match.player_profiles||(match.players||[]).map(name=>({name}))).filter(Boolean)){const item=el('span','player-chip'),guest=person.registered===false;item.append(identityAvatar(person,guest),el('span','',person.name||'Speler'));if(guest){item.title='Discord-speler · niet geregistreerd';item.append(el('span','visually-hidden',' (niet geregistreerd)'));}roster.append(item);}return roster;}
function message(text,error=false,busy=false){clearTimeout(messageTimer);const box=$('status');box.replaceChildren();box.className='status toast'+(error?' error':'')+(busy?' busy':'');box.hidden=!text;box.setAttribute('role',error?'alert':'status');if(!text)return;box.append(el('span','',text));const close=el('button','toast-close','\u00d7');close.type='button';close.setAttribute('aria-label','Melding sluiten');close.addEventListener('click',()=>message(''));box.append(close);if(!busy)messageTimer=setTimeout(()=>{if(box.textContent===text+'\u00d7')message('');},error?14000:7000);}
function feedback(id,text,error=false){const box=$(id);if(!box)return;box.textContent=text;box.classList.toggle('error',error);box.setAttribute('role',error?'alert':'status');}
async function api(path,method='GET',body,timeoutMs=30000){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeoutMs);try{const r=await fetch(path,{method,signal:controller.signal,credentials:'same-origin',headers:{'Content-Type':'application/json',...(me?{'X-CSRF-Token':me.csrf}:{})},...(body===undefined?{}:{body:JSON.stringify(body)})});let data;try{data=await r.json();}catch(error){if(controller.signal.aborted)throw error;throw new Error('De server gaf geen geldig antwoord. Probeer opnieuw.');}if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'Controleer je invoer en probeer opnieuw.');if(data.error)throw new Error(data.error);return data;}catch(error){if(controller.signal.aborted)throw new Error('De aanvraag duurt te lang. Controleer de voortgang en probeer opnieuw.');throw error;}finally{clearTimeout(timer);}}
async function guarded(fn,localId){try{await fn();}catch(e){if(localId)feedback(localId,e.message,true);if($('device-dialog').open)$('device-message').textContent=e.message;else message(e.message,true);}}
async function waitForJob(job_id,text='KUL controleren...',timeoutMs=740000){message(text,false,true);const deadline=Date.now()+timeoutMs;for(let i=0;Date.now()<deadline;i++){await new Promise(r=>setTimeout(r,Math.min(2000,Math.max(1,deadline-Date.now()))));if(Date.now()>=deadline)break;const job=await api('/api/jobs/'+job_id,'GET',undefined,Math.min(30000,Math.max(1,deadline-Date.now())));if(job.status==='done'){message('');return job.result;}if(['failed','unknown','cancelled'].includes(job.status))throw new Error(job.result?.error||'De taak kon niet worden afgerond.');if(i===8)message('Nog even geduld. De KUL-login wordt gecontroleerd.',false,true);}const error=new Error('De controle loopt nog. Je kunt de voortgang opnieuw ophalen.');error.pending=true;throw error;}
async function task(path,body={},text='Even ophalen bij KU Leuven Sport...'){message(text,false,true);const queued=await api(path,'POST',body);if(!queued.job_id){message('');return queued;}return waitForJob(queued.job_id,text);}
function empty(container,title,text){container.replaceChildren();const box=el('div','empty');box.append(el('span','empty-icon','◫'),el('h3','',title),el('p','',text));container.append(box);}
function linkedState(id,linked,text){const node=$(id);node.textContent=(linked?'\u2713 ':'')+text;node.classList.toggle('is-linked',linked);}
function accountNeedsCheck(){const check=me.account_status?.check;return !!(me.needs_attention||me.account_status?.error||(check&&check.status!=='done'&&check.checked>(me.payment?.checked||0)));}
function hasLocalCard(){return !!me?.payment?.local_card&&me.payment.stored_card===true&&!me.payment.local_card.expired;}
function mergePayment(incoming,current=me?.payment){if(!incoming)return current;const local=Object.hasOwn(incoming,'local_card')?{local_card:incoming.local_card,stored_card:!!incoming.local_card&&incoming.stored_card!==false&&!incoming.local_card.expired}:{local_card:current?.local_card||null,stored_card:!!current?.local_card&&current.stored_card!==false&&!current.local_card.expired};return {...(current&&Number(current.checked||0)>Number(incoming.checked||0)?current:{...current,...incoming}),...local};}
function applyMe(updatePreferences=true){
 $('profile-name').textContent=me.name.split(' ')[0]+(me.owner?' \u00b7 Beheerder':'');$('avatar').replaceChildren(identityAvatar(me));$('admin-nav').hidden=me.owner!==true;
 if(me.owner!==true||!me.admin){adminLoadEpoch++;$('admin-users').replaceChildren();clearAdminMetrics();}
 const needsCheck=accountNeedsCheck(),checking=!!me.account_status?.checking,verified=me.kul_verified===true,kulState=checking?'Controleren...':verified?'Gekoppeld':needsCheck||me.device_ready||me.connected?'Controle nodig':'Niet gekoppeld';linkedState('device-state',verified,kulState);$('start-device').textContent=me.device_ready?'Koppeling bekijken':'KUL koppelen';
 document.querySelector('[data-open-setting="kul"]').textContent=me.connected?'Beheren':'Koppelen';
 linkedState('setup-kul-state',verified,kulState);
 linkedState('setup-discord-state',me.discord_linked,me.discord_linked?'Gekoppeld':'Niet gekoppeld');linkedState('discord-status',me.discord_linked,me.discord_linked?'Gekoppeld':'Niet gekoppeld');
 $('add-discord').textContent=me.discord_linked?'Beheren':'Koppelen \u2197';$('add-discord').href=me.discord_linked?'#settings':'/auth/discord?personal=true';
 $('discord-fallback').hidden=!!me.discord_oauth||!!me.discord_linked;$('link-discord').hidden=!!me.discord_linked;
 if(me.discord_linked)$('discord-link-code').hidden=true;
 if(updatePreferences){$('auto-pay').checked=!!me.preferences.automatic_payment;$('max-amount').value=me.preferences.max_amount||0;$('payment-mode').value=me.preferences.payment_mode||'discord';}
 const hasCard=hasLocalCard();linkedState('setup-payment-state',me.preferences.payment_mode==='kul'&&hasCard,me.preferences.payment_mode==='discord'?'Betaallink':me.preferences.payment_mode==='kul'?(hasCard?'Kaart gekoppeld':'Kaart nodig'):'Kies je betaalwijze');
 $('autobook-card-state').textContent=hasCard?'\u2713 Kaart gekoppeld':'Voeg een kaart toe voor autobooking.';$('autobook-card-state').classList.toggle('is-linked',hasCard);
 const p=me.payment,box=$('payment-state');box.replaceChildren();
 const balanceKnown=p&&p.balance!==null&&p.balance!==undefined&&Number.isFinite(Number(p.balance));
 $('kul-balance').textContent=balanceKnown?euro(p.balance):'Nog niet opgehaald';
 $('setup-payment-balance').textContent=balanceKnown?'KUL-saldo '+euro(p.balance):'';
 $('setup-payment-balance').hidden=!balanceKnown;
 $('kul-balance-checked').textContent=p?.checked?'Gecontroleerd om '+clock(new Date(p.checked*1000).toISOString()):'';
 if(hasCard)box.append(el('p','help is-linked','\u2713 Standaardkaart gekoppeld'));
}
async function loadMe(updatePreferences=true){const updated=await api('/api/me');updated.payment=mergePayment(updated.payment);me=updated;applyMe(updatePreferences);}
function availabilityRoute(value){const match=/^slots\/([0-6])-([0-6])$/.exec(value);return match&&Number(match[1])<=Number(match[2])?{start:Number(match[1]),end:Number(match[2]),fragment:match[0]}:null;}
function setSlotRange(start,end){
 $('range-start').value=start;$('range-end').value=end;
 document.querySelectorAll('[data-range]').forEach(button=>button.classList.toggle('selected',button.dataset.range===start+','+end));
}
function rememberSlotRange(){
 const route=availabilityRoute('slots/'+$('range-start').value+'-'+$('range-end').value);if(!route)throw new Error('Kies dagen tussen 0 en 6.');
 availabilityDestination=route.fragment;history.replaceState(null,'','#'+route.fragment);
}
function updateLoginDestination(){const route=availabilityRoute(location.hash.slice(1)),button=document.querySelector('#login .google-button');button.href=route?'/auth/google?next='+encodeURIComponent('/#'+route.fragment):'/auth/google';}
async function showPage(page){
 const availability=availabilityRoute(page);if(availability){availabilityDestination=availability.fragment;setSlotRange(availability.start,availability.end);page='dashboard';}
 else if(page==='dashboard'||page==='slots'||page.startsWith('slots/')){availabilityDestination=null;setSlotRange(0,0);}
 const invite=page.match(/^match\/([a-f0-9]{32})$/);if(invite){invitedMatch=invite[1];inviteScrolled=false;inviteRefreshTried=false;communityFilter='all';page='online';}else if(page!=='online')invitedMatch=null;
 const panel={settings:'profile-settings',rules:'autobook-panel',bookings:'my-bookings-panel'}[page];
 if(panel)page='dashboard';if(page==='slots')page='dashboard';
 if(!titles[page]||(page==='admin'&&me?.owner!==true))page='dashboard';activePage=page;
 for(const section of document.querySelectorAll('.page'))section.hidden=section.id!=='page-'+page;
 for(const button of document.querySelectorAll('nav [data-page]'))button.classList.toggle('active',button.dataset.page===page);
 $('page-title').textContent=titles[page];history.replaceState(null,'',page==='online'&&invitedMatch?'#match/'+invitedMatch:page==='dashboard'&&availabilityDestination?'#'+availabilityDestination:'#'+page);message('');
 if(panel){$(panel).open=true;$(panel).scrollIntoView({block:'start',behavior:'smooth'});}
 if(page==='online')await loadOnline();if(page==='admin')await loadAdmin();
 if(page==='dashboard'){void guarded(loadPayments);if(me.connected){if(slotsCache&&slotsCacheDay===brusselsDay())renderSlots();else if(!slotsLoading)void guarded(()=>slots());}}
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
function slotsSkeleton(){const skeleton=el('div','slots-skeleton');skeleton.setAttribute('aria-hidden','true');for(let i=0;i<3;i++){const row=el('div');row.append(el('span'),el('span'),el('span'));skeleton.append(row);}$('slots').replaceChildren(skeleton);$('slot-summary').textContent='7 dagen ophalen...';}
async function slots(refresh=false){
 if(!me.connected){$('slot-summary').textContent='';$('slots').replaceChildren(el('p','empty-line','Koppel KUL om vrije momenten te zien.'));return;}
 if(slotsCacheDay!==brusselsDay())slotsCache=null;
 if(slotsLoading){await slotsLoading;renderSlots();return;}
 if(!slotsCache||refresh){$('refresh-slots').disabled=true;$('refresh-slots').setAttribute('aria-busy','true');$('slots').setAttribute('aria-busy','true');if(!slotsCache)slotsSkeleton();
  slotsLoading=(async()=>{const data=await task('/api/slots',{start:0,end:6},'Vrije momenten voor 7 dagen ophalen...');slotsCache=data.slots||[];slotsCacheDay=brusselsDay();})();
  try{await slotsLoading;}catch(error){if(!slotsCache){$('slot-summary').textContent='';empty($('slots'),'Ophalen niet gelukt',error.message);}throw error;}finally{slotsLoading=null;$('refresh-slots').disabled=false;$('refresh-slots').setAttribute('aria-busy','false');$('slots').setAttribute('aria-busy','false');}
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
 const data=await task('/api/actions/bookings');
 const [sharing,cancellations]=await Promise.all([api('/api/community/mine'),api('/api/bookings/cancellations')]);bookingSharing=sharing.shared||{};
 for(const [id,state] of bookingCancellations)if(!state.busy&&state.status!=='done'&&!cancellations.cancellations?.[id])bookingCancellations.delete(id);
 for(const [id,state] of Object.entries(cancellations.cancellations||{}))if(!bookingCancellations.get(id)?.busy)bookingCancellations.set(id,{...state,message:state.error||(['queued','running','waiting_auth'].includes(state.status)?'Annulering wordt verwerkt.':state.status==='done'?'Geannuleerd bij KUL. Vernieuw je boekingen.':state.status==='unknown'?'De annulering is niet bevestigd. Controleer je boekingen bij KUL.':'')});
 bookingsCache=data.bookings||[];renderBookings();
}
function renderBookings(){
 const visible=bookingsCache.filter(item=>bookingCancellations.get(String(item.id))?.status!=='done');
 if(!visible.length){empty($('bookings'),'Geen komende boekingen','');return;}
 $('bookings').replaceChildren();
 for(const b of visible){const row=el('div','list-item'),text=el('div');text.append(el('h3','',date(b.start)),el('p','',clock(b.start)+' - '+clock(b.end)),el('small','',b.name||b.description));row.append(text);const actions=el('div','actions');actions.append(el('span','badge',b.confirmed?'Bevestigd':'Niet bevestigd'));
  if(b.confirmed){let shared=!!bookingSharing[b.id];const toggle=el('button','button light',shared?'Offline zetten':'Online zetten'),share=el('button','button light','Delen');toggle.addEventListener('click',()=>guarded(async()=>{toggle.disabled=true;try{await api('/api/community/share','POST',{booking_id:b.id,shared:!shared});shared=!shared;bookingSharing[b.id]=shared;toggle.textContent=shared?'Offline zetten':'Online zetten';}finally{toggle.disabled=false;}}));share.addEventListener('click',()=>guarded(async()=>{share.disabled=true;try{const match=await api('/api/community/share','POST',{booking_id:b.id,shared:true});shared=true;bookingSharing[b.id]=true;toggle.textContent='Offline zetten';await shareMatch(match.id,match.url);}finally{share.disabled=false;}}));actions.append(toggle,share);}
  if(new Date(b.start).getTime()>Date.now()&&b.cancellable!==false){const state=bookingCancellations.get(String(b.id));if(state?.status==='done')actions.append(el('span','badge','Geannuleerd'));else{const cancel=el('button','text-button danger-link',state?.job_id?'Annulering controleren':'Annuleren');cancel.type='button';cancel.addEventListener('click',()=>openCancellation(b));actions.append(cancel);}}
  row.append(actions);$('bookings').append(row);
 }
}
function cancellationFeedback(id,text,error=false){feedback('bookings-feedback',text,error);if(String(selectedCancellation?.id)===id)feedback('cancel-booking-feedback',text,error);}
function cancellationControls(state){$('confirm-cancel-booking').hidden=state?.status==='unknown';$('cancel-booking-kul').hidden=state?.status!=='unknown';$('confirm-cancel-booking').textContent=state?.job_id?'Status controleren':'Ja, annuleren';$('confirm-cancel-booking').disabled=!!state?.busy;$('confirm-cancel-booking').setAttribute('aria-busy',String(!!state?.busy));}
function openCancellation(item){
 selectedCancellation=item;const state=bookingCancellations.get(String(item.id));$('cancel-booking-detail').replaceChildren(el('strong','',date(item.start)),el('p','',clock(item.start)+' - '+clock(item.end)),el('small','',item.name||item.description||'Padel'));
 feedback('cancel-booking-feedback',state?.message||'',state?.status==='unknown');cancellationControls(state);
 if(!$('cancel-booking-dialog').open)$('cancel-booking-dialog').showModal();
}
async function confirmCancellation(){
 const item=selectedCancellation;if(!item)return;const id=String(item.id);let state=bookingCancellations.get(id);if(state?.busy||state?.status==='unknown')return;
 state={...state,busy:true,message:'Annuleren bij KUL...'};bookingCancellations.set(id,state);$('confirm-cancel-booking').disabled=true;$('confirm-cancel-booking').setAttribute('aria-busy','true');cancellationFeedback(id,state.message);
 try{
  let result;if(!state.job_id){const queued=await api('/api/bookings/'+encodeURIComponent(id)+'/cancel','POST',{});state.job_id=queued.job_id;result=state.job_id?null:queued;}
  if(state.job_id){const deadline=Date.now()+740000;while(Date.now()<deadline){const job=await api('/api/jobs/'+encodeURIComponent(state.job_id));state.status=job.status;if(job.status==='done'){result=job.result;break;}if(['failed','unknown','cancelled'].includes(job.status))throw new Error(job.result?.error||'De annulering is niet bevestigd. Vernieuw je boekingen om de status te controleren.');await new Promise(resolve=>setTimeout(resolve,2000));}if(!result)throw new Error('De annulering loopt nog. Controleer de status opnieuw.');}
  if(result?.payment){me.payment=mergePayment({...result.payment,checked:result.payment_checked_at||result.payment.checked||0});applyMe(false);}
  if(Array.isArray(result?.bookings)){bookingsCache=result.bookings;renderBookings();}
  if(result?.cancelled!==true)throw new Error(result?.message||'De annulering is nog niet bevestigd. Controleer de status opnieuw.');
  if(!Array.isArray(result.bookings))bookingsCache=bookingsCache.filter(item=>String(item.id)!==id);
  delete bookingSharing[id];state.status='done';renderBookings();state.message=result.message||(result.payment?'\u2713 Geannuleerd. KUL-saldo vernieuwd.':'\u2713 Geannuleerd. Saldo controleren...');cancellationFeedback(id,state.message);
  if(String(selectedCancellation?.id)===id){$('cancel-booking-dialog').close();selectedCancellation=null;}
  slotsCache=null;slotsCacheDay=null;const refreshes=[loadMe(false),loadPayments(),api('/api/community/mine').then(data=>{bookingSharing=data.shared||{};renderBookings();})];if(activePage==='online')refreshes.push(loadOnline());if(result.refresh_required||!result.payment)refreshes.push(checkAccount(true,false));if(!Array.isArray(result.bookings))refreshes.push(bookings());if(activePage==='dashboard'&&me.connected)refreshes.push(slots(true));
  const refreshed=await Promise.allSettled(refreshes);if(refreshed.some(result=>result.status==='rejected'))feedback('bookings-feedback','\u2713 Geannuleerd. Niet alle gegevens konden worden vernieuwd. Gebruik Vernieuwen.');
 }catch(error){state.message=error.message;cancellationFeedback(id,state.message,true);}
 finally{state.busy=false;if(String(selectedCancellation?.id)===id)cancellationControls(state);}
}
async function loadOnline(){const data=await api('/api/community');const container=$('online-matches');container.replaceChildren();if(invitedMatch&&!data.matches.some(match=>match.id===invitedMatch)&&!inviteRefreshTried){inviteRefreshTried=true;const refresh=await api('/api/community/'+invitedMatch+'/refresh','POST',{});if(refresh.refreshing){container.append(el('p','empty-line','Match controleren...'));message('Match controleren...',false,true);return true;}}document.querySelectorAll('[data-community-filter]').forEach(button=>button.classList.toggle('selected',button.dataset.communityFilter===communityFilter));const matches=data.matches.filter(match=>communityFilter==='all'||match.kind===communityFilter);if(!matches.length){if(invitedMatch&&data.refreshing){container.append(el('p','empty-line','Match controleren...'));return data.refreshing;}empty(container,communityFilter==='draft'?'Geen open drafts':'Geen open matches','Maak een draft vanuit Beschikbaarheid of deel een bevestigde reservatie via Mijn boekingen.');if(communityFilter!=='confirmed')await loadOwnDraftIssues(container,data.matches);return data.refreshing;}for(const match of matches){const row=el('div','list-item match-card');row.dataset.matchId=match.id;if(match.id===invitedMatch)row.classList.add('invited-match');const info=el('div');const head=el('div','match-card-head');head.append(el('h3','',match.title),el('span','player-count',match.count+'/'+match.capacity));info.append(head,el('span','badge '+(match.kind==='draft'?'draft-badge':''),match.kind==='draft'?'Voorstel':'Bevestigd'),el('p','match-date',date(match.start)+' - '+clock(match.start)+' tot '+clock(match.end)),playerRoster(match));if(match.kind==='draft')info.append(el('small','','Verloopt '+date(new Date(match.expires*1000).toISOString())+' om '+clock(new Date(match.expires*1000).toISOString())));else info.append(el('small','','Gecontroleerd om '+clock(new Date(match.verified*1000).toISOString())+(match.origin==='discord'?' - Ook op Discord':'')));row.append(info);const actions=el('div','match-actions');if(match.organiser){if(match.kind==='draft'){const pay=el('button','button dark','Boeken en betalen');pay.addEventListener('click',()=>guarded(()=>draftQuote(match)));const stop=el('button','button light','Stoppen');stop.addEventListener('click',()=>guarded(async()=>{await task('/api/drafts/'+match.id+'/cancel',{},'Eigen draft-item opruimen...');await loadOnline();}));actions.append(pay,stop);}else actions.append(el('span','badge','Organisator'));}else{const button=el('button','button '+(match.joined?'light':'dark'),match.joined?'Uitschrijven':'Deelnemen');button.addEventListener('click',()=>guarded(async()=>{button.disabled=true;try{if(match.joined)await api('/api/community/'+match.id+'/leave','POST',{});else await task('/api/community/'+match.id+'/join',{},match.kind==='draft'?'Aanmelden voor voorstel...':'Reservering bij KUL controleren...');await loadOnline();}finally{button.disabled=false;}}));actions.append(button);}const share=el('button','button light','Delen');share.addEventListener('click',()=>guarded(()=>shareMatch(match.id)));actions.append(share);row.append(actions);container.append(row);}if(invitedMatch&&!inviteScrolled){const invited=Array.from(container.children).find(row=>row.dataset.matchId===invitedMatch);if(invited){invited.scrollIntoView({block:'center',behavior:'smooth'});inviteScrolled=true;message('');}else message('Deze match is niet meer beschikbaar.');}if(communityFilter!=='confirmed')await loadOwnDraftIssues(container,data.matches);return data.refreshing;}

async function loadOwnDraftIssues(container,visible){const data=await api('/api/drafts/mine');const shown=new Set(visible.map(match=>match.id));const states={pending:'Draft wordt aangemaakt',uncertain:'Controle nodig',stopping:'Draft wordt opgeruimd',cleanup_failed:'Controleer je KUL-winkelwagen',payment_pending:'Controleer je KUL-boeking en betaling',converting:'Boeking wordt verwerkt',cancelled:'Draft gestopt',expired:'Draft verlopen',unavailable:'Terrein niet meer beschikbaar'};for(const draft of data.drafts){if(shown.has(draft.id)||['cancelled','expired','unavailable'].includes(draft.state))continue;const row=el('div','list-item');const info=el('div');info.append(el('h3','',draft.title),el('p','',date(draft.start)+' - '+clock(draft.start)),el('span','badge',states[draft.state]||'Controle nodig'));if(draft.error)info.append(el('p','help',draft.error));row.append(info);if(['uncertain','cleanup_failed'].includes(draft.state)){const stop=el('button','button light','Stoppen');stop.addEventListener('click',()=>guarded(async()=>{await task('/api/drafts/'+draft.id+'/cancel',{},'Eigen draft-item opruimen...');await loadOnline();}));row.append(stop);}else if(['payment_pending','converting'].includes(draft.state)){const link=el('a','button light','Controleer bij KUL');link.href='https://usc.kuleuven.cloud/';link.target='_blank';link.rel='noopener';row.append(link);}container.append(row);}}

async function refreshOnline(){await api('/api/community/refresh','POST',{});message('Reserveringen bij KUL controleren...',false,true);for(let i=0;i<65;i++){await new Promise(r=>setTimeout(r,2000));if(activePage!=='online')return;const pending=await loadOnline();if(!pending){message('');return;}}message('De controle loopt nog. Vernieuw over een ogenblik.');}
function detectCardNetwork(value){const digits=String(value||'').replace(/\D/g,''),first4=Number(digits.slice(0,4));if(/^6703/.test(digits))return 'bancontact';if(/^4/.test(digits))return 'visa';if(/^3[47]/.test(digits))return 'amex';if(/^5[1-5]/.test(digits)||digits.length>=4&&first4>=2221&&first4<=2720)return 'mastercard';if(/^(5018|5020|5038|5893|6304|6759|6761|6762|6763)/.test(digits))return 'maestro';return 'card';}
const cardNetworkNames={visa:'Visa',mastercard:'Mastercard',amex:'American Express',bancontact:'Bancontact',maestro:'Maestro',card:'Kaart'};
function cardSupportsCheckout(card){return ['visa','mastercard','bancontact','maestro'].includes(savedCardNetwork(card))&&!card.expired;}
function savedCardNetwork(card){const value=String(card.network||card.brand||'').toLowerCase().replace(/[\s_-]/g,'');return ({americanexpress:'amex',dinersclub:'diners',mastercard:'mastercard'})[value]||(cardNetworkNames[value]?value:'card');}
function cardNetworkIcon(network){
 const icon=el('span','card-network-icon '+network);icon.setAttribute('role','img');icon.setAttribute('aria-label',cardNetworkNames[network]||'Kaart');
 const marks={mastercard:'<circle cx="22" cy="18" r="11" fill="#eb001b"/><circle cx="35" cy="18" r="11" fill="#f79e1b" fill-opacity=".9"/>',maestro:'<circle cx="22" cy="18" r="11" fill="#eb001b"/><circle cx="35" cy="18" r="11" fill="#0099df" fill-opacity=".9"/>',visa:'<text x="28" y="24" fill="#1434cb" font-family="Arial,sans-serif" font-style="italic" font-weight="bold" font-size="18" text-anchor="middle">VISA</text>',amex:'<rect x="5" y="4" width="46" height="28" rx="3" fill="#0875be"/><text x="28" y="22" fill="#fff" font-family="Arial,sans-serif" font-weight="bold" font-size="12" text-anchor="middle">AMEX</text>',bancontact:'<path d="M8 9h27L22 27H8z" fill="#005498"/><path d="M48 27H21L34 9h14z" fill="#f9c127"/>',card:'<g fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="7" width="38" height="23" rx="4"/><path d="M9 14h38M15 23h9"/></g>'};
 const fallback=marks.card;icon.innerHTML='<svg viewBox="0 0 56 36" aria-hidden="true">'+(marks[network]||fallback)+'</svg>';return icon;
}
function updateCardNetwork(){const network=detectCardNetwork($('card-number').value),box=$('card-network');box.replaceChildren(cardNetworkIcon(network));box.dataset.network=network;}
function resetCardForm(){const form=$('card-form');form.reset();form.querySelector('details').open=false;$('card-number').setCustomValidity('');feedback('payment-dialog-feedback','');updateCardNetwork();}
async function loadCard(){
 const epoch=++cardLoadEpoch,data=await api('/api/payment-card');if(epoch!==cardLoadEpoch)return;
 const cards=data.cards||[],container=$('saved-card');container.replaceChildren();cardVaultEnabled=data.enabled!==false;$('manage-payment').disabled=!cardVaultEnabled;
 const primary=cards.find(card=>card.primary)||data.card||null;
 me.payment={...me.payment,local_card:primary,stored_card:!!primary&&cardSupportsCheckout(primary)};applyMe(false);
 if(!cardVaultEnabled){container.append(el('p','help','Kaarten toevoegen is tijdelijk niet beschikbaar.'));return;}
 if(!cards.length){container.append(el('p','help','Nog geen kaarten.'));return;}
 for(const card of cards){
  const row=el('div','wallet-card'),identity=el('div','wallet-card-identity'),info=el('div','wallet-card-info'),network=savedCardNetwork(card),heading=el('div','wallet-card-heading');row.dataset.cardId=card.id;
  heading.append(el('strong','',card.label||cardNetworkNames[network]));if(card.primary)heading.append(el('span','badge','Standaard'));if(card.expired)heading.append(el('span','badge warning','Verlopen'));else if(!cardSupportsCheckout(card))heading.append(el('span','badge','Handmatig betalen'));
  info.append(heading,el('p','wallet-card-number',cardNetworkNames[network]+' \u2022\u2022\u2022\u2022 '+card.last4));const funding={credit:'Creditcard',debit:'Debitcard'}[card.funding];info.append(el('small','',(funding?funding+' \u00b7 ':'')+String(card.month).padStart(2,'0')+'/'+card.year));identity.append(cardNetworkIcon(network),info);
  const actions=el('div','wallet-card-actions');
  if(!card.primary&&!card.expired){const makePrimary=el('button','button light','Als standaard');makePrimary.type='button';makePrimary.addEventListener('click',()=>guarded(async()=>{makePrimary.disabled=true;try{await api('/api/payment-card/'+card.id+'/primary','POST',{});await loadCard();await loadMe(false);feedback('card-feedback','\u2713 Standaardkaart gewijzigd.');}finally{makePrimary.disabled=false;}},'card-feedback'));actions.append(makePrimary);}
  const remove=el('button','text-button danger-link','Verwijderen');remove.type='button';remove.addEventListener('click',()=>guarded(async()=>{remove.disabled=true;try{await api('/api/payment-card/'+card.id,'DELETE',{});await loadCard();await loadMe(false);feedback('card-feedback','Kaart verwijderd.');}finally{remove.disabled=false;}},'card-feedback'));actions.append(remove);row.append(identity,actions);container.append(row);
 }
}
async function saveCard(event){
 event.preventDefault();if(cardSaving)return;const number=$('card-number').value.replace(/[ -]/g,'');
 if(!/^\d{12,19}$/.test(number)){$('card-number').setCustomValidity('Vul een geldig kaartnummer in.');$('card-number').reportValidity();return;}
 cardSaving=true;$('save-card').disabled=true;$('card-form').setAttribute('aria-busy','true');feedback('payment-dialog-feedback','Kaart bewaren...');
 try{await api('/api/payment-card','POST',{holder:$('card-holder').value,number,month:Number($('card-month').value),year:Number($('card-year').value),label:$('card-label').value,primary:$('card-primary').checked,funding:$('card-funding').value});resetCardForm();$('payment-dialog').close();await loadCard();await loadMe(false);feedback('card-feedback','\u2713 Kaart bewaard.');feedback('payment-feedback',hasLocalCard()?'\u2713 Kaart gekoppeld.':'Kaart bewaard.');}
 catch(error){feedback('payment-dialog-feedback',error.message,true);}
 finally{cardSaving=false;$('save-card').disabled=false;$('card-form').removeAttribute('aria-busy');}
}

async function loadRules(){const data=await api('/api/rules');if(!data.rules.length){empty($('rules'),'Geen wekelijkse regels','Voeg een wekelijks speelmoment toe.');return;}$('rules').replaceChildren();for(const rule of data.rules){const row=el('div','list-item');const text=el('div');text.append(el('h3','',days[rule.day_of_week]),el('p','',rule.time_local+(rule.end_time_local&&rule.end_time_local!==rule.time_local?' – '+rule.end_time_local:'')),el('small','',rule.notes||'Wekelijks'));const actions=el('div','actions');const toggle=el('button','text-button',rule.enabled?'Pauzeren':'Activeren');toggle.addEventListener('click',()=>guarded(async()=>{await api('/api/rules/'+rule.id+'/toggle','POST',{});await loadRules();}));const remove=el('button','text-button danger-link','Verwijderen');remove.addEventListener('click',()=>guarded(async()=>{await api('/api/rules/'+rule.id,'DELETE');await loadRules();}));actions.append(toggle,remove);row.append(text,actions);$('rules').append(row);}}
async function activity(){const data=await api('/api/history');$('activity').replaceChildren();const names={slots:'Vrije momenten',login:'KUL koppelen',account:'Account',bookings:'Boekingen',cancel_booking:'Annulering',rule:'Autobooking',book:'Boeking',quote:'Prijs',community_join:'Deelname',draft_refresh:'Voorstel',draft_release:'Voorstel stoppen'};const states={queued:'In de wachtrij',running:'Bezig',waiting_auth:'KUL opnieuw aanmelden',done:'Klaar',failed:'Mislukt',unknown:'Controle nodig',cancelled:'Geannuleerd'};if(!data.jobs.length){$('activity').append(el('p','help','Nog geen activiteit.'));return;}for(const j of data.jobs){const row=el('div','activity-row'),info=el('div'),error=j.result?.error;info.append(el('strong','',names[j.kind]||j.kind),el('small',error?'activity-error':'',error||j.result?.message||states[j.status]||j.status));const value=j.finished||j.created,time=el('time');if(value){const iso=new Date(Number(value)*1000).toISOString();time.dateTime=iso;time.textContent=new Date(iso).toLocaleDateString('nl-BE',{day:'numeric',month:'short'})+' \u00b7 '+clock(iso);}row.append(info,time);$('activity').append(row);}}
function openPaymentDialog(){if(!cardVaultEnabled){feedback('card-feedback','Kaarten toevoegen is tijdelijk niet beschikbaar.',true);return;}if(!$('payment-dialog').open){resetCardForm();$('payment-dialog').showModal();$('card-holder').focus();}}
function accountFeedback(data){me.payment=mergePayment(data.payment);me.account_status=data;const checking=data.checking===true,failed=data.check&&['failed','unknown','cancelled'].includes(data.check.status)&&data.check.checked>=(me.payment?.checked||0),error=data.error||(failed?(data.check.error||'KUL kon niet worden gecontroleerd.'):null)||(me.needs_attention?'Controleer je KUL-koppeling.':null);applyMe(false);if(checking){feedback('kul-feedback','KUL wordt automatisch gecontroleerd...');return;}if(error){feedback('kul-feedback',error,true);return;}const checked=data.check?.checked||data.payment?.checked,stamp=checked?' om '+clock(new Date(checked*1000).toISOString()):'';feedback('kul-feedback',me.kul_verified===true?'\u2713 Gekoppeld'+(stamp?' \u00b7 gecontroleerd'+stamp:'')+'.':me.device_ready?'Koppeling nog niet bevestigd.':'Koppel KUL om te boeken.');}
async function checkAccount(force=false){
 if(!me||!me.device_ready||$('device-dialog').open)return;if(accountCheckFlight)return accountCheckFlight;if(!force&&Date.now()-accountCheckedAt<60000)return;
 accountCheckedAt=Date.now();accountCheckFlight=(async()=>{try{let state=await api('/api/account/check','POST',force?{force:true}:{});accountFeedback(state);const deadline=Date.now()+120000;while(state.checking&&Date.now()<deadline){await new Promise(resolve=>setTimeout(resolve,3000));state=await api('/api/account/status');accountFeedback(state);}if(state.checking){feedback('kul-feedback','De controle loopt nog. Je kunt de site verder gebruiken.');}else{await loadMe(false);accountFeedback(state);if(me.connected&&!slotsCache&&!slotsLoading)void guarded(()=>slots());}await activity();}catch(error){feedback('kul-feedback',error.message,true);}finally{accountCheckFlight=null;}})();return accountCheckFlight;
}
function adminStamp(value){const stamp=typeof value==='string'?new Date(value):new Date(Number(value)*1000);return value&&Number.isFinite(stamp.getTime())?stamp.toLocaleDateString('nl-BE',{timeZone:'Europe/Brussels',day:'numeric',month:'short'})+' \u00b7 '+clock(stamp.toISOString()):null;}
function adminBadge(text,kind=''){return el('span','badge'+(kind?' '+kind:''),text);}
function adminField(label,value){const row=el('div','admin-field');row.append(el('dt','',label));const detail=el('dd');detail.append(typeof value==='string'?document.createTextNode(value):value);row.append(detail);return row;}
function adminSection(title){const section=el('section','admin-section');section.append(el('h4','',title));return section;}
function clearAdminMetrics(){adminMetricsEpoch++;adminMetricsLoading=false;adminMetricsLoaded=false;$('admin-system').hidden=true;$('admin-metrics').replaceChildren();$('admin-metrics').removeAttribute('aria-busy');$('refresh-admin-metrics').disabled=false;}
function metricNumber(value){return typeof value==='number'&&Number.isFinite(value)&&value>=0?value:null;}
function metricCount(value){const number=metricNumber(value);return number===null?'\u2014':Math.floor(number).toLocaleString('nl-BE');}
function metricDuration(value){const number=metricNumber(value);if(number===null)return '\u2014';if(number<60)return Math.round(number)+' s';if(number<3600)return Math.round(number/60)+' min';if(number<86400)return (number/3600).toLocaleString('nl-BE',{maximumFractionDigits:1})+' uur';return (number/86400).toLocaleString('nl-BE',{maximumFractionDigits:1})+' dagen';}
function metricCard(label,value,note){const card=el('div','admin-metric-card');card.append(el('span','',label),el('strong','',value));if(note)card.append(el('small','',note));return card;}
function renderAdminMetrics(data){
 const box=$('admin-metrics');box.replaceChildren();if(data.enabled===false){box.append(el('p','help','Meting is uitgeschakeld.'));return;}if(data.available===false){box.append(el('p','local-feedback error','Meetgegevens zijn tijdelijk niet beschikbaar.'));return;}
 const totals=data.totals||{},performance=data.performance||{},cadence=data.cadence||{},window=data.window||{},cards=el('div','admin-metric-cards'),observed=adminStamp(window.observed_since),heading=el('div','admin-metric-window');heading.append(el('p','help',observed?'Gemeten sinds '+observed+' \u00b7 laatste '+metricCount(window.days||30)+' dagen.':'De meting is gestart. Nog geen activiteit gemeten.'));
 const vm=data.auth_vm;if(vm){const state=vm.status||vm.state||vm.powerstate,label={running:'Authenticator aan',stopped:'Authenticator uit',starting:'Authenticator start',stopping:'Authenticator sluit af',unknown:'Toestelstatus onbekend'}[state]||'Toestelstatus onbekend';heading.append(adminBadge(label,state==='running'?'is-linked':''));}box.append(heading);
 cards.append(metricCard('Toestelstarts',metricCount(totals.emulator_starts),metricCount(totals.enrollment_starts)+' koppelen \u00b7 '+metricCount(totals.reauth_starts)+' heraanmelden'),metricCard('MFA geslaagd',metricCount(totals.mfa_successes),metricCount(totals.mfa_failures)+' mislukt'),metricCard('Zonder toestel',metricCount(totals.silent_successes),'Geslaagde sessievernieuwingen'));box.append(cards);
 const cadenceBox=el('div','admin-metric-cadence'),sampleCount=metricNumber(cadence.sample_count)||0;cadenceBox.append(el('strong','','Tijd tussen heraanmeldingen'));if(sampleCount>0&&metricNumber(cadence.median_seconds)!==null){cadenceBox.append(el('p','',metricDuration(cadence.median_seconds)+' mediaan'),el('small','',metricCount(sampleCount)+(sampleCount===1?' gemeten interval ':' gemeten intervallen ')+'\u00b7 '+metricDuration(cadence.min_seconds)+' tot '+metricDuration(cadence.max_seconds)));}else cadenceBox.append(el('p','help','Nog te weinig heraanmeldingen om een interval te meten.'));if(cadence.last_success_at)cadenceBox.append(el('small','','Laatste geslaagde MFA: '+adminStamp(cadence.last_success_at)));box.append(cadenceBox);
 const fields=el('dl','admin-metric-performance');fields.append(adminField('Toesteltijd',metricDuration(totals.native_seconds)),adminField('Gemiddeld',metricDuration(performance.mean_native_seconds)),adminField('P95',metricDuration(performance.p95_native_seconds)),adminField('Wachten op emulator',metricDuration(performance.mean_queue_wait_seconds)),adminField('Emulator-RAM-piek',metricNumber(performance.peak_rss_mb)===null?'\u2014':Math.round(performance.peak_rss_mb).toLocaleString('nl-BE')+' MB'));box.append(fields,el('p','help','Wachttijd: exclusief taakwachtrij en VM-opstart. RAM: alleen het emulatorproces.'));
 const daily=(data.daily||[]).filter(day=>day&&/^\d{4}-\d{2}-\d{2}$/.test(day.date)).sort((a,b)=>a.date.localeCompare(b.date)).slice(-7);if(daily.length){const wrapper=el('div','admin-metric-daily'),table=el('table'),caption=el('caption','','Laatste 7 dagen'),head=el('thead'),headRow=el('tr');for(const label of ['Dag','Starts','MFA','Toesteltijd']){const cell=el('th','',label);cell.scope='col';headRow.append(cell);}head.append(headRow);table.append(caption,head);const body=el('tbody');for(const day of daily){const row=el('tr'),label=new Date(day.date+'T12:00:00Z').toLocaleDateString('nl-BE',{timeZone:'Europe/Brussels',day:'numeric',month:'short'}),cell=el('th','',label);cell.scope='row';row.append(cell,el('td','',metricCount(day.emulator_starts)),el('td','',metricCount(day.mfa_successes)),el('td','',metricDuration(day.native_seconds)));body.append(row);}table.append(body);wrapper.append(table);box.append(wrapper);}
}
async function loadAdminMetrics(force=false){
 if(me?.owner!==true||!me.admin){clearAdminMetrics();return;}if(adminMetricsLoading||adminMetricsLoaded&&!force)return;const epoch=++adminMetricsEpoch;adminMetricsLoading=true;$('refresh-admin-metrics').disabled=true;$('admin-metrics').setAttribute('aria-busy','true');$('admin-metrics').replaceChildren(el('p','help','Metingen ophalen...'));
 try{const data=await api('/api/admin/metrics');if(epoch!==adminMetricsEpoch||me?.owner!==true||!me.admin)return;renderAdminMetrics(data);adminMetricsLoaded=true;}catch(error){if(epoch!==adminMetricsEpoch)return;$('admin-metrics').replaceChildren(el('p','local-feedback error',error.message));}finally{if(epoch===adminMetricsEpoch){adminMetricsLoading=false;$('refresh-admin-metrics').disabled=false;$('admin-metrics').removeAttribute('aria-busy');}}
}
function adminMatchRows(items,emptyText,{checked,joins=false}={}){
 const list=el('div','admin-match-list');if(!items.length){list.append(el('p','help',emptyText));return list;}
 for(const item of items){const row=el('div','admin-match-row'),info=el('div');info.append(el('strong','',item.title||item.name||'Padel'));
  if(item.start)info.append(el('p','',date(item.start)+' \u00b7 '+clock(item.start)+(item.end?' - '+clock(item.end):'')));
  if(joins&&item.host)info.append(el('small','','Bij '+item.host));const stamp=adminStamp(item.verified||checked);if(stamp)info.append(el('small','','Gecontroleerd '+stamp));row.append(info);
  const badges=el('div','admin-row-badges');if(joins){if(Number.isFinite(Number(item.count)))badges.append(adminBadge(item.count+'/'+(item.capacity||4)));const status={open:'Open',draft:'Voorstel',cancelled:'Geannuleerd',closed:'Gesloten',full:'Volzet'}[item.status];if(status)badges.append(adminBadge(status));}
  else{badges.append(adminBadge(item.confirmed?'Bevestigd':'Niet bevestigd',item.confirmed?'is-linked':''));if(item.shared)badges.append(adminBadge('Gedeeld'));}row.append(badges);list.append(row);
 }return list;
}
function adminModeration(detail,guest,refresh){
 const profile=detail.profile,form=el('form','admin-moderation'),title=el('h4','','Toegang');form.append(title);
 for(const [field,label,help] of [['suspended','Geblokkeerd','Geen toegang tot de app.'],['restricted','Alleen lezen','Geen nieuwe boekingen, voorstellen of deelnames.']]){
  const wrapper=el('div'),caption=el('label','checkbox'),input=el('input');input.type='checkbox';input.name=field;input.checked=!!profile[field];caption.append(input,document.createTextNode(label));wrapper.append(caption,el('small','',help));form.append(wrapper);
 }
 const actions=el('div','button-row'),save=el('button','button dark','Toegang bewaren'),status=el('p','local-feedback');save.type='submit';status.setAttribute('role','status');actions.append(save);form.append(actions,status);
 form.addEventListener('submit',async event=>{event.preventDefault();save.disabled=true;status.textContent='Bewaren...';status.classList.remove('error');try{await api('/api/admin/'+(guest?'guests/':'users/')+encodeURIComponent(profile.id),'PATCH',{suspended:form.elements.suspended.checked,restricted:form.elements.restricted.checked});await refresh();message('Toegang bewaard.');}catch(error){status.textContent=error.message;status.classList.add('error');status.setAttribute('role','alert');}finally{save.disabled=false;}});return form;
}
function renderAdminDetail(detail,guest,refresh){
 const container=el('div','admin-detail-grid'),profile=detail.profile||{},discord=detail.discord||{},kul=detail.kul||{},payment=detail.payment||{},connections=adminSection('Koppelingen'),fields=el('dl','admin-fields');
 const kulState=kul.needs_attention?'Controle nodig':kul.verified?'Gekoppeld':kul.registered?'Registratie bewaard':'Niet gekoppeld';fields.append(adminField('KUL',adminBadge(kulState,kul.verified?'is-linked':kul.needs_attention?'warning':'')));
 const discordAccount=el('div','admin-discord-account');if(discord.id){const identity=el('a','',discord.name||'Discord-speler');identity.href='https://discord.com/users/'+encodeURIComponent(discord.id);identity.target='_blank';identity.rel='noopener noreferrer';discordAccount.append(identity,el('small','',String(discord.id)));}else discordAccount.append(el('span','','Niet gekoppeld'));fields.append(adminField('Discord',discordAccount));
 const paymentState=payment.checked?(payment.linked?'Gekoppeld':'Geen gekoppelde kaart'):'Nog niet gecontroleerd',paymentInfo=el('div');paymentInfo.append(adminBadge(paymentState,payment.checked&&payment.linked?'is-linked':''));if(payment.checked)paymentInfo.append(el('small','','Gecontroleerd '+adminStamp(payment.checked)));fields.append(adminField('Creditcard',paymentInfo));connections.append(fields);container.append(connections);
 const installs=adminSection('Discord'),personal=discord.personal_install==='known'?'Via persoonlijk profiel gebruikt':'Profielinstallatie niet bevestigd';installs.append(el('p','admin-install-state',personal),el('h5','','Bekende serverinstallaties'));
 if(discord.guilds?.length){const list=el('div','admin-guilds');for(const guild of discord.guilds){const row=el('div','admin-guild'),icon=identityAvatar({name:guild.name||'Server',avatar_url:guild.icon_url}),text=el('div');text.append(el('strong','',guild.name||'Server'),el('small','',String(guild.id)));const stamp=adminStamp(guild.observed);if(stamp)text.append(el('small','','Gezien '+stamp));if(guild.installed===false)text.append(el('small','','Niet actief'));row.append(icon,text);list.append(row);}installs.append(list);}else installs.append(el('p','help',discord.guilds_checked?'Geen serverinstallaties bekend.':'Nog niet vastgesteld.'));
 if(discord.guilds_checked)installs.append(el('p','help','Bijgewerkt '+adminStamp(discord.guilds_checked)));container.append(installs);
 const bookingsSection=adminSection('Boekingen'),bookingsChecked=detail.bookings_checked||null;bookingsSection.append(adminMatchRows(detail.bookings||[],bookingsChecked?'Geen opgeslagen komende boekingen.':'Nog niet gecontroleerd.',{checked:bookingsChecked}));if(bookingsChecked)bookingsSection.append(el('p','help','Laatste controle '+adminStamp(bookingsChecked)));
 if(detail.pending_payments?.length){bookingsSection.append(el('h5','','Nog te betalen'));for(const item of detail.pending_payments){const text=el('p','help',item.booking_id?'Boeking '+item.booking_id:'Betaling '+item.sale_id);const stamp=adminStamp(item.created);if(stamp)text.append(document.createTextNode(' \u00b7 '+stamp));bookingsSection.append(text);}}container.append(bookingsSection);
 const rules=adminSection('Autobooking');if(!detail.rules?.length)rules.append(el('p','help','Geen wekelijkse regels.'));else for(const rule of detail.rules){const row=el('div','admin-rule'),info=el('div');info.append(el('strong','',(days[rule.day_of_week]||'Wekelijks')+' \u00b7 '+rule.time_local+(rule.end_time_local&&rule.end_time_local!==rule.time_local?' - '+rule.end_time_local:'')));if(rule.notes)info.append(el('small','',rule.notes));row.append(info,adminBadge(rule.enabled?'Regel aan':'Gepauzeerd',rule.enabled?'is-linked':'paused'));rules.append(row);}container.append(rules);
 const joins=adminSection('Deelnames');joins.append(adminMatchRows(detail.joins||[],'Geen opgeslagen deelnames.',{joins:true}));container.append(joins);
 if(!profile.owner)container.append(adminModeration(detail,guest,refresh));return container;
}
function adminEntry(person,guest){
 const entry=el('details','admin-person'),summary=el('summary'),identity=el('div','admin-person-identity'),text=el('div','admin-person-name');entry.dataset.adminId=(guest?'guest:':'user:')+person.id;
 text.append(el('strong','',person.name||'Discord-speler'),el('small','user-email',guest?'Discord \u00b7 niet geregistreerd':person.email||'Geregistreerd profiel'));identity.append(identityAvatar(person,guest),text);summary.append(identity);
 const badges=el('span','admin-person-badges');if(person.owner)badges.append(adminBadge('Beheerder'));if(person.suspended)badges.append(adminBadge('Geblokkeerd','warning'));else if(person.restricted)badges.append(adminBadge('Alleen lezen','paused'));summary.append(badges);entry.append(summary);const body=el('div','admin-person-body');entry.append(body);let loading=false,loaded=false;
 async function loadDetail(){if(loading)return;loading=true;body.replaceChildren(el('p','help','Profiel ophalen...'));try{const detail=await api('/api/admin/'+(guest?'guests/':'users/')+encodeURIComponent(person.id));body.replaceChildren(renderAdminDetail(detail,guest,loadAdmin));loaded=true;}catch(error){const status=el('p','local-feedback error',error.message),retry=el('button','button light','Opnieuw proberen');status.setAttribute('role','alert');retry.type='button';retry.addEventListener('click',loadDetail);body.replaceChildren(status,retry);}finally{loading=false;}}
 entry.addEventListener('toggle',()=>{if(entry.open&&!loaded)void loadDetail();});return entry;
}
async function loadAdmin(){
 const epoch=++adminLoadEpoch,container=$('admin-users');if(me?.owner!==true){container.replaceChildren();clearAdminMetrics();return;}$('admin-gate').hidden=me.admin;if(!me.admin){container.replaceChildren();clearAdminMetrics();return;}
 $('admin-system').hidden=false;if($('admin-system').open)void loadAdminMetrics(true);
 const expanded=new Set([...container.querySelectorAll('details[open]')].map(entry=>entry.dataset.adminId));let data;try{data=await api('/api/admin/users');}catch(error){if(epoch===adminLoadEpoch){container.replaceChildren();clearAdminMetrics();}throw error;}if(epoch!==adminLoadEpoch||me?.owner!==true||!me.admin)return;
 container.replaceChildren();const users=data.users||[],guests=data.guests||[],heading=el('div','admin-list-heading');heading.append(el('h2','','Profielen'),el('span','section-label',String(users.length)));container.append(heading);
 for(const person of users){const entry=adminEntry(person,false);container.append(entry);if(expanded.has(entry.dataset.adminId))entry.open=true;}
 if(!users.length)container.append(el('p','help','Nog geen profielen.'));if(guests.length){const heading=el('div','admin-list-heading');heading.append(el('h2','','Discord-spelers'),el('span','section-label',String(guests.length)));container.append(heading);for(const person of guests){const entry=adminEntry(person,true);container.append(entry);if(expanded.has(entry.dataset.adminId))entry.open=true;}}
}
let pairPhase='waiting',pairReady=false,pairPolling=false,pairSaving=false,pairSubmitted=false,pairStepNumber=1,pairFinished=false,pendingRegistrationFile=null,registrationSending=false,registrationEpoch=0,registrationPreviewSequence=0,pairStateError=false,pairLoginJob=null;
if(!$('device-pin-confirm')){const label=el('label','','PIN bevestigen'),input=el('input');input.id='device-pin-confirm';input.type='password';input.inputMode='numeric';input.pattern='[0-9]{4}';input.maxLength=4;input.autocomplete='new-password';input.required=true;input.disabled=true;label.append(input);$('finish-device').before(label);}
$('device-advanced')?.remove();$('pin-check-label')?.remove();
const eyeIcon='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>';
for(const id of ['device-pin','device-pin-confirm']){const input=$(id);if(!document.querySelector('[data-pin-toggle="'+id+'"]')){const wrapper=el('div','pin-input'),button=el('button','pin-eye');input.before(wrapper);wrapper.append(input,button);button.type='button';button.dataset.pinToggle=id;}const button=document.querySelector('[data-pin-toggle="'+id+'"]');button.innerHTML=eyeIcon;button.setAttribute('aria-label',id==='device-pin'?'PIN tonen':'PIN bevestiging tonen');button.setAttribute('aria-pressed','false');button.addEventListener('click',()=>{const visible=input.type==='password';input.type=visible?'text':'password';button.setAttribute('aria-pressed',String(visible));button.setAttribute('aria-label',(id==='device-pin'?'PIN':'PIN bevestiging')+(visible?' verbergen':' tonen'));});}
function clearPairPins(){for(const id of ['device-pin','device-pin-confirm']){$(id).value='';$(id).type='password';const button=document.querySelector('[data-pin-toggle="'+id+'"]');button.setAttribute('aria-pressed','false');button.setAttribute('aria-label',(id==='device-pin'?'PIN':'PIN bevestiging')+' tonen');}}
function pairButton(busy=false,label='Koppelen'){const button=$('finish-device');button.disabled=busy||!pairReady||pairFinished;button.textContent=label;button.setAttribute('aria-busy',String(busy));}
function clearRegistrationPreview(){registrationPreviewSequence++;pendingRegistrationFile=null;$('qr-preview').hidden=true;$('qr-preview-image').removeAttribute('src');$('qr-preview-state').textContent='';$('device-qr').value='';}
function pairStep(step){pairStepNumber=step;document.querySelectorAll('[data-pair-step]').forEach(x=>x.hidden=Number(x.dataset.pairStep)!==step);document.querySelectorAll('[data-pair-marker]').forEach(x=>{if(Number(x.dataset.pairMarker)===step)x.setAttribute('aria-current','step');else x.removeAttribute('aria-current');});}
function pairProgress(text=''){const progress=$('pair-progress');progress.textContent=text;progress.hidden=!text;}
function showPairResult(state,text=''){
 pairFinished=true;clearInterval(deviceTimer);clearRegistrationPreview();clearPairPins();pairProgress();$('device-message').textContent='';$('retry-device').hidden=true;$('pair-again').hidden=state!=='warning'||!me.device_ready;
 document.querySelectorAll('[data-pair-step]').forEach(section=>section.hidden=true);document.querySelector('.pair-steps').hidden=true;$('device-dialog').classList.add('is-complete');
 const complete=$('pair-complete');complete.dataset.state=state;complete.hidden=false;$('pair-complete-title').textContent=state==='connected'?'Gekoppeld':state==='warning'?'Controle nodig':'Koppeling controleren';$('pair-complete-text').textContent=text;complete.setAttribute('role',state==='warning'?'alert':'status');
}
async function refreshPairProfile(){await loadMe(false);accountFeedback(me.account_status||{});if(me.connected&&!slotsCache&&!slotsLoading)void guarded(()=>slots());}
async function deviceCommand(action,extra={}){return api('/api/device/control','POST',{action,...extra},90000);}
async function pollEnrollment(){
 if(pairPolling||pairSaving||pairFinished||!$('device-dialog').open)return;pairPolling=true;const epoch=registrationEpoch;
 try{
  const result=await api('/api/device/status');if(epoch!==registrationEpoch||pairSaving||!$('device-dialog').open)return;pairReady=result.ready===true;
  $('device-qr').disabled=registrationSending||pairSaving;$('upload-qr').disabled=registrationSending||pairSaving;$('paste-qr').disabled=registrationSending||pairSaving;$('send-device-link').disabled=!pairReady||registrationSending||pairSaving;
  if(result.state==='failed')throw new Error(result.error||'Koppelen is niet gelukt. Probeer opnieuw.');
  if(result.state==='idle')throw new Error('De koppelsessie is afgelopen. Probeer opnieuw.');
  $('retry-device').hidden=true;
  if(!pairReady){pairProgress();return;}
  if(pendingRegistrationFile&&!registrationSending){try{await flushRegistration();}catch(error){$('device-message').textContent=error.message;}return;}
  pairPhase=result.phase||'waiting';
  const prompts={enroll_pin:'Vul je PIN in.',needs_registration:'',waiting:pairSubmitted?'Bevestig zo nodig op je eigen telefoon. We wachten op KUL.':'',pin_create:'',pin_confirm:'',registered:'Rond de koppeling af.',pin_rejected:'KUL weigert deze PIN. Kies een minder voorspelbare PIN.',login_qr:'Dit is een aanmeld-QR. Gebruik in KUL-accountbeheer Toestel toevoegen.'};
  pairProgress(prompts[pairPhase]||'');
  const canFinish=['pin_create','pin_confirm','enroll_pin','registered'].includes(pairPhase);
  $('device-pin').disabled=$('device-pin-confirm').disabled=!pairSubmitted&&!canFinish;pairButton();
  if(canFinish){pairStep(3);$('pair-pin-title').textContent=pairPhase==='pin_create'?'Kies een PIN':'Toestel-PIN';$('pair-pin-help').textContent='4 cijfers.';if(pairStateError){$('device-message').textContent='';pairStateError=false;}}
  if(pairPhase==='login_qr'){pairStep(2);pairSubmitted=false;}
 }catch(error){if(epoch!==registrationEpoch)return;pairReady=false;pairButton();pairProgress();$('device-message').textContent=error.message;$('retry-device').hidden=false;clearInterval(deviceTimer);$('retry-device').scrollIntoView({block:'nearest'});}
 finally{if(epoch===registrationEpoch)pairPolling=false;}
}
async function startDevice(newRegistration=false){
 pairStateError=false;pairSaving=false;pairPolling=false;pairLoginJob=null;pairButton();
 clearInterval(deviceTimer);registrationEpoch++;clearRegistrationPreview();registrationSending=false;pairFinished=false;$('pair-complete').hidden=true;document.querySelector('.pair-steps').hidden=false;$('device-dialog').classList.remove('is-complete');$('qr-feedback').textContent='';pairReady=false;pairSubmitted=false;pairPhase='waiting';pairStep(1);clearPairPins();$('device-message').textContent='';$('retry-device').hidden=true;$('retry-device').textContent='Opnieuw proberen';$('pair-again').hidden=true;$('device-qr').disabled=false;$('upload-qr').disabled=false;$('paste-qr').disabled=false;$('send-device-link').disabled=true;$('device-pin').disabled=$('device-pin-confirm').disabled=true;$('finish-device').disabled=true;
 if(!$('device-dialog').open)$('device-dialog').showModal();
 pairProgress();
 const epoch=registrationEpoch;
 try{
  await loadMe(false);if(epoch!==registrationEpoch||!$('device-dialog').open)return;
  if(!newRegistration&&(me.device_ready||me.kul_verified)){
   if(me.kul_verified===true){showPairResult('connected','Klaar om te boeken.');return;}
   if(me.needs_attention){showPairResult('warning','Je koppeling is bewaard. De KUL-aanmelding vraagt controle.');return;}
   showPairResult('checking','Je bestaande koppeling wordt gecontroleerd.');
   const state=await api('/api/account/check','POST',{force:true});if(epoch!==registrationEpoch||!$('device-dialog').open)return;accountFeedback(state);
   if(state.error)throw new Error(state.error);
   if(state.check?.job_id){pairLoginJob=state.check.job_id;void checkPairLogin();return;}
   throw new Error('Je koppeling kon nog niet worden gecontroleerd. Probeer opnieuw.');
  }
  await api('/api/device/start','POST',{});if(epoch!==registrationEpoch||!$('device-dialog').open)return;await pollEnrollment();deviceTimer=setInterval(pollEnrollment,2000);
 }catch(error){if(epoch!==registrationEpoch)return;if(pairFinished)showPairResult('warning',error.message);else $('device-message').textContent=error.message;$('retry-device').hidden=false;}
}
async function closeDevice(){registrationEpoch++;pairSaving=false;pairPolling=false;pairButton();clearRegistrationPreview();clearInterval(deviceTimer);deviceTimer=null;clearPairPins();$('device-link').value='';$('device-dialog').close();try{await deviceCommand('stop');}catch{}try{await refreshPairProfile();if(me.account_status?.checking)void checkAccount(true);}catch{}}
function clipboardImage(data){for(const file of Array.from(data?.files||[]))if(file.type.startsWith('image/'))return file;for(const item of Array.from(data?.items||[])){if(item.kind==='file'&&item.type.startsWith('image/')){const file=item.getAsFile();if(file)return file;}}return null;}
async function sendRegistration(file){
 if(pairSaving||registrationSending)throw new Error('De vorige afbeelding wordt nog verwerkt.');
 if(!file.size||file.size>8000000)throw new Error('Gebruik een screenshot van maximaal 8 MB.');
 const epoch=registrationEpoch,sequence=++registrationPreviewSequence;pendingRegistrationFile=null;
 const preview=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('De afbeelding kon niet worden gelezen.'));reader.readAsDataURL(file);});
 if(epoch!==registrationEpoch||sequence!==registrationPreviewSequence||!$('device-dialog').open)return;
 pendingRegistrationFile={encoded:preview.split(',')[1]};pairStep(2);$('qr-preview-image').src=preview;$('qr-preview').hidden=false;$('qr-preview-state').textContent='Afbeelding toegevoegd';$('remove-qr').disabled=false;$('qr-preview').scrollIntoView({block:'nearest',behavior:'smooth'});$('device-message').textContent='';$('qr-feedback').textContent=pairReady?'Afbeelding ontvangen. QR-code zoeken...':'Afbeelding ontvangen. Je QR wordt zo verwerkt.';
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
  $('qr-feedback').textContent='QR-code ontvangen.';$('qr-preview-state').textContent='QR-code gevonden en verstuurd';pairSubmitted=true;$('device-pin').disabled=$('device-pin-confirm').disabled=false;pairButton();$('device-message').textContent='';pairProgress('Registratie verstuurd. We wachten op KUL.');pairStep(3);await pollEnrollment();
 }catch(error){if(epoch!==registrationEpoch)return;$('qr-feedback').textContent='';$('qr-preview-state').textContent='Afbeelding niet verwerkt';throw error;}
 finally{if(epoch===registrationEpoch){registrationSending=false;$('device-qr').value='';$('device-qr').disabled=false;$('upload-qr').disabled=false;$('paste-qr').disabled=false;$('remove-qr').disabled=false;}}
}
async function finishEnrollment(event){
 event.preventDefault();if(pairSaving||pairFinished)return;
 const pin=$('device-pin').value,confirmation=$('device-pin-confirm').value;
 if(!/^[0-9]{4}$/.test(pin)||pin!==confirmation){$('device-message').textContent='Vul tweemaal dezelfde PIN van vier cijfers in.';$('device-pin-confirm').focus();return;}
 pairSaving=true;pairStateError=false;const epoch=registrationEpoch;pairButton(true,'Koppelen...');
 try{
  $('device-message').textContent='';pairProgress('Koppeling afronden...');
  let state=await api('/api/device/status');
  const deadline=Date.now()+5000;
  while(state.ready&&['waiting','needs_registration'].includes(state.phase)&&Date.now()<deadline){await new Promise(resolve=>setTimeout(resolve,600));if(epoch!==registrationEpoch)return;state=await api('/api/device/status');}
  if(epoch!==registrationEpoch)return;
  if(!state.ready)throw new Error('De koppelsessie is afgelopen. Probeer opnieuw.');
  pairPhase=state.phase||'waiting';
  if(!['pin_create','pin_confirm','enroll_pin','registered'].includes(pairPhase)){
   pairStateError=true;throw new Error(pairPhase==='pin_rejected'?'KUL heeft de PIN niet aanvaard.':'KUL verwerkt de registratie nog. Probeer opnieuw zodra de PIN-stap klaar is.');
  }
  if(pairPhase!=='registered'){await deviceCommand('setup_pin',{pin,confirm_pin:confirmation});if(epoch!==registrationEpoch)return;}
  pairButton(true,'Registratie bewaren...');
  const result=await deviceCommand('finish',{pin});if(!result.saved)throw new Error('KUL heeft de registratie nog niet bevestigd.');
  if(epoch!==registrationEpoch)return;pairLoginJob=result.job_id;showPairResult('checking','Je koppeling is bewaard. KUL-login controleren...');
  try{await refreshPairProfile();}catch{}
  void checkPairLogin();
 }catch(error){if(epoch!==registrationEpoch)return;throw error;}
 finally{if(epoch===registrationEpoch){clearPairPins();pairSaving=false;pairButton();if($('device-dialog').open&&!pairFinished)await pollEnrollment();}}
}
async function checkPairLogin(timeoutMs=120000){
 if(!pairLoginJob)return;const epoch=registrationEpoch;
 try{
  const deadline=Date.now()+timeoutMs;
  while(Date.now()<deadline){
   await new Promise(resolve=>setTimeout(resolve,2000));if(epoch!==registrationEpoch||!$('device-dialog').open)return;
   const job=await api('/api/jobs/'+pairLoginJob);
   if(job.status==='done'){await refreshPairProfile();if(epoch!==registrationEpoch)return;pairLoginJob=null;if(me.kul_verified!==true&&job.result?.connected!==true)throw new Error('Je koppeling is bewaard, maar de KUL-aanmelding is nog niet bevestigd.');showPairResult('connected','Klaar om te boeken.');return;}
   if(['failed','unknown','cancelled'].includes(job.status))throw new Error(job.result?.error||'KUL-login controleren is niet gelukt.');
  }
  const error=new Error('De controle loopt nog. Je kunt dit venster sluiten.');error.pending=true;throw error;
 }catch(error){if(epoch!==registrationEpoch)return;showPairResult(error.pending?'checking':'warning',error.pending?error.message:'Je koppeling is bewaard. '+error.message);if(error.pending){$('retry-device').textContent='Voortgang ophalen';$('retry-device').hidden=false;}}
 finally{if(epoch===registrationEpoch){try{await refreshPairProfile();await activity();}catch{}}}
}

document.querySelectorAll('[data-page]').forEach(b=>b.addEventListener('click',()=>guarded(()=>showPage(b.dataset.page))));
document.querySelectorAll('[data-range]').forEach(b=>b.addEventListener('click',()=>guarded(async()=>{const [a,z]=b.dataset.range.split(',');setSlotRange(Number(a),Number(z));rememberSlotRange();await slots();})));
$('refresh-slots').addEventListener('click',()=>guarded(()=>slots(true)));$('apply-range').addEventListener('click',()=>guarded(async()=>{rememberSlotRange();await slots();}));$('refresh-payments').addEventListener('click',()=>guarded(loadPayments));$('refresh-bookings').addEventListener('click',()=>guarded(bookings));$('confirm-book').addEventListener('click',()=>guarded(confirmBook));
$('confirm-cancel-booking').addEventListener('click',()=>void confirmCancellation());
$('cancel-booking-dialog').addEventListener('cancel',event=>event.preventDefault());
$('rule-form').addEventListener('submit',e=>{e.preventDefault();guarded(async()=>{await api('/api/rules','POST',{day_of_week:Number($('rule-day').value),time_local:$('rule-start').value,end_time_local:$('rule-end').value,notes:$('rule-notes').value});await loadRules();feedback('rule-feedback','\u2713 Moment bewaard.');message('Moment bewaard.');},'rule-feedback');});
$('payment-form').addEventListener('submit',e=>{e.preventDefault();guarded(async()=>{if(($('payment-mode').value==='kul'||$('auto-pay').checked)&&!hasLocalCard()){feedback('payment-feedback','Voeg eerst een kaart toe.',true);feedback('autobook-feedback','Voeg een kaart toe voor autobooking.',true);openPaymentDialog();return;}const buttons=[...document.querySelectorAll('button[form="payment-form"],#payment-form button[type="submit"]')];buttons.forEach(button=>button.disabled=true);try{await api('/api/preferences','POST',{automatic_payment:$('auto-pay').checked,max_amount:Number($('max-amount').value),payment_mode:$('payment-mode').value});await loadMe();feedback('payment-feedback','\u2713 Bewaard.');feedback('autobook-feedback','\u2713 Bewaard.');message('Bewaard.');}catch(error){feedback('payment-feedback',error.message,true);feedback('autobook-feedback',error.message,true);throw error;}finally{buttons.forEach(button=>button.disabled=false);}},'payment-feedback');});
$('payment-mode').addEventListener('change',()=>{feedback('payment-feedback','');if($('payment-mode').value==='kul'&&!hasLocalCard())openPaymentDialog();});
$('auto-pay').addEventListener('change',()=>{feedback('autobook-feedback','');if(!$('auto-pay').checked)return;$('payment-mode').value='kul';if(!hasLocalCard()){$('auto-pay').checked=false;feedback('autobook-feedback','Voeg een kaart toe voor autobooking.',true);openPaymentDialog();}});
$('manage-payment').addEventListener('click',openPaymentDialog);
for(let year=new Date().getFullYear();year<=new Date().getFullYear()+20;year++){$('card-year').add(new Option(String(year),String(year)));}
$('card-form').addEventListener('submit',saveCard);
$('card-number').addEventListener('input',()=>{$('card-number').setCustomValidity('');updateCardNetwork();});$('card-number').addEventListener('change',updateCardNetwork);
$('payment-dialog').addEventListener('close',resetCardForm);$('payment-dialog').addEventListener('cancel',event=>event.preventDefault());
updateCardNetwork();
$('link-discord').addEventListener('click',()=>guarded(async()=>{const result=await api('/api/discord/link','POST',{});$('discord-command').textContent=result.command;$('discord-link-code').hidden=false;}));$('copy-discord-link').addEventListener('click',()=>guarded(async()=>{await navigator.clipboard.writeText($('discord-command').textContent);message('Commando gekopieerd.');}));
document.querySelectorAll('[data-community-filter]').forEach(button=>button.addEventListener('click',()=>guarded(async()=>{communityFilter=button.dataset.communityFilter;await loadOnline();})));$('create-draft').addEventListener('click',()=>guarded(createDraft));
$('refresh-online').addEventListener('click',()=>guarded(refreshOnline));
$('logout').addEventListener('click',()=>guarded(async()=>{await api('/auth/logout','POST',{});location.href='/';}));$('verify-admin').addEventListener('click',()=>guarded(async()=>{const r=await api('/api/admin/access','POST',{});location.href=r.url;}));
$('admin-system').addEventListener('toggle',()=>{if($('admin-system').open)void loadAdminMetrics();});$('refresh-admin-metrics').addEventListener('click',()=>void loadAdminMetrics(true));
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>$(b.dataset.close).close()));
$('start-device').addEventListener('click',()=>guarded(startDevice));$('retry-device').addEventListener('click',()=>guarded(startDevice));$('close-device').addEventListener('click',closeDevice);$('close-device-footer').addEventListener('click',closeDevice);$('device-dialog').addEventListener('cancel',e=>{e.preventDefault();e.stopPropagation();});$('device-dialog').addEventListener('click',e=>{if(e.target===$('device-dialog')){e.preventDefault();e.stopPropagation();}});
$('pair-again').addEventListener('click',()=>guarded(()=>startDevice(true)));
$('pair-next').addEventListener('click',()=>pairStep(2));$('pair-back').addEventListener('click',()=>pairStep(1));$('pair-new-qr').addEventListener('click',()=>pairStep(2));
$('link-form').addEventListener('submit',e=>{e.preventDefault();guarded(async()=>{await deviceCommand('link',{link:$('device-link').value});$('device-link').value='';pairSubmitted=true;$('device-message').textContent='';pairStep(3);await pollEnrollment();});});
$('pin-form').addEventListener('submit',e=>guarded(()=>finishEnrollment(e)));
$('device-qr').addEventListener('change',()=>guarded(async()=>{const file=$('device-qr').files[0];if(file)await sendRegistration(file);}));
$('upload-qr').addEventListener('click',()=>$('device-qr').click());
$('remove-qr').addEventListener('click',()=>{if(registrationSending||pairSaving)return;clearRegistrationPreview();$('qr-feedback').textContent='';$('device-message').textContent='';});
$('paste-qr').addEventListener('click',()=>{$('paste-qr').focus();$('qr-feedback').textContent='Druk nu Ctrl+V om je screenshot te plakken.';});
document.addEventListener('paste',e=>{if(!$('device-dialog').open||pairSaving||pairFinished)return;const file=clipboardImage(e.clipboardData);if(file){e.preventDefault();guarded(()=>sendRegistration(file));}else if(e.target===$('paste-qr'))$('qr-feedback').textContent='Geen afbeelding op je klembord. Maak eerst een screenshot met Win+Shift+S.';});
function savedInvite(){try{return sessionStorage.getItem('padel-invite');}catch{return null;}}
function rememberInvite(value){try{if(value)sessionStorage.setItem('padel-invite',value);else sessionStorage.removeItem('padel-invite');}catch{}}
(async()=>{try{await loadMe();$('app').hidden=false;$('today').textContent=new Date().toLocaleDateString('nl-BE',{weekday:'short',day:'numeric',month:'long'});const destination=location.hash.slice(1)||savedInvite()||'dashboard';rememberInvite(null);await guarded(()=>showPage(destination));void checkAccount();}catch(e){const invite=location.hash.slice(1);if(/^match\/[a-f0-9]{32}$/.test(invite))rememberInvite(invite);updateLoginDestination();$('login').hidden=false;if(!e.message.includes('Meld je aan'))console.warn('Profiel is nog niet beschikbaar.');}})();

setInterval(()=>{if(me&&activePage==='online'&&!document.hidden)guarded(async()=>{await loadOnline();});},15000);

for(const button of document.querySelectorAll('[data-open-setting]'))button.addEventListener('click',()=>guarded(async()=>{await showPage('settings');$(button.dataset.openSetting==='kul'?'kul-settings':'payment-form').scrollIntoView({block:'center',behavior:'smooth'});}));
$('profile-settings').addEventListener('toggle',()=>{if($('profile-settings').open)guarded(async()=>{await Promise.all([loadCard(),activity()]);});});
$('autobook-panel').addEventListener('toggle',()=>{if($('autobook-panel').open)guarded(loadRules);});

$('copy-share').addEventListener('click',()=>guarded(async()=>{await navigator.clipboard.writeText($('share-url').value);$('share-feedback').textContent='Link gekopieerd.';}));
$('native-share').addEventListener('click',()=>guarded(async()=>{try{await navigator.share({title:'Padel',text:'Wie doet er mee?',url:$('share-url').value});}catch(error){if(error.name!=='AbortError')throw error;}}));
window.addEventListener('hashchange',()=>{if(me)guarded(()=>showPage(location.hash.slice(1)||'dashboard'));else updateLoginDestination();});
let refreshingMe=false;
async function refreshAccess(){if(!me||document.hidden||refreshingMe)return;refreshingMe=true;try{const current=await api('/api/me'),previousAdmin=me.admin;me.owner=current.owner;me.admin=current.admin;me.admin_eligible=current.admin_eligible;$('admin-nav').hidden=me.owner!==true;if(!me.owner||!me.admin){adminLoadEpoch++;$('admin-users').replaceChildren();clearAdminMetrics();}if(activePage==='admin'){if(!me.owner)await showPage('dashboard');else if(previousAdmin!==me.admin)await loadAdmin();}}catch{}finally{refreshingMe=false;}}
window.addEventListener('focus',refreshAccess);document.addEventListener('visibilitychange',()=>{if(!document.hidden)void refreshAccess();});setInterval(refreshAccess,30000);
function onAccountReturn(){if(!me||document.hidden)return;if(Date.now()-accountCheckedAt>300000)void checkAccount();}
window.addEventListener('focus',onAccountReturn);document.addEventListener('visibilitychange',()=>{if(!document.hidden)onAccountReturn();});
for(const tip of document.querySelectorAll('.info-tip')){const button=tip.querySelector('button');button.addEventListener('click',()=>{const open=button.getAttribute('aria-expanded')!=='true';document.querySelectorAll('.info-tip button').forEach(item=>item.setAttribute('aria-expanded','false'));button.setAttribute('aria-expanded',String(open));});button.addEventListener('keydown',event=>{if(event.key==='Escape'){button.setAttribute('aria-expanded','false');button.blur();}});}
document.addEventListener('click',event=>{if(!event.target.closest('.info-tip'))document.querySelectorAll('.info-tip button').forEach(button=>button.setAttribute('aria-expanded','false'));});
