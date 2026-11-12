let current=[];
const $=id=>document.getElementById(id);
async function api(url,opts={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...opts});const d=await r.json();if(!r.ok)throw Error(d.error||'Ошибка');return d}
function riskClass(r){return r>=65?'high':r>=35?'medium':'low'}
async function refresh(){
 const d=await api('/api/state');current=d.shipments;
 $('total').textContent=d.stats.total;$('high').textContent=d.stats.high;$('avg').innerHTML=d.stats.avg_risk+'<em>%</em>';$('co2').innerHTML=d.stats.co2+'<em> кг</em>';$('esgTotal').textContent=d.stats.co2+' кг';
 $('shipmentRows').innerHTML=current.map(s=>`<tr><td><b>${s.id}</b><small>${s.cargo}</small></td><td>${s.origin} → ${s.destination}</td><td>${s.mode}</td><td><span class="pill">${s.status}</span></td><td><span class="risk ${riskClass(s.forecast.risk)}">${s.forecast.risk}% · ${s.forecast.level}</span><small>+${s.forecast.delay_hours} ч (оценка)</small></td><td>${s.co2}</td><td><button class="small-btn" onclick="selectFor('${s.id}')">Прогноз</button><button class="small-btn" onclick="route('${s.id}')">Обход</button></td></tr>`).join('');
 const opts=current.map(s=>`<option value="${s.id}">${s.id} · ${s.cargo}</option>`).join('');
 $('predictId').innerHTML=opts;$('customsId').innerHTML=opts;
 renderEvents(d.events);$('updated').textContent=new Date().toLocaleTimeString('ru-RU');
}
function renderEvents(events){$('events').innerHTML=events.slice().reverse().map(e=>`<div class="event"><b>${e.action} · ${e.shipment}</b><small>${e.time} — ${e.detail}</small><code>HASH ${e.hash}</code></div>`).join('')}
function selectFor(id){$('predictId').value=id;const s=current.find(x=>x.id===id);if(s){$('weather').value=s.weather;$('weatherVal').textContent=s.weather;$('congestion').value=s.congestion;$('congestionVal').textContent=s.congestion;$('rail').value=s.rail_delay;$('railVal').textContent=s.rail_delay;location.hash='#forecast'}}
async function runPrediction(){try{const d=await api('/api/predict',{method:'POST',body:JSON.stringify({id:$('predictId').value,weather:$('weather').value,congestion:$('congestion').value,rail_delay:$('rail').value})});$('predictionResult').innerHTML=`<b>Риск задержки: <span class="risk ${riskClass(d.forecast.risk)}">${d.forecast.risk}% · ${d.forecast.level}</span></b><br>Ожидаемая дополнительная задержка: <b>${d.forecast.delay_hours} часов</b><br><small>Факторы: погода ${d.shipment.weather}%, загруженность ${d.shipment.congestion}%, ж/д задержка ${d.shipment.rail_delay} мин.</small>`;await refresh()}catch(e){alert(e.message)}}
async function clearCustoms(){try{const d=await api('/api/customs/'+$('customsId').value,{method:'POST',body:'{}'});$('customsMessage').textContent='✓ '+d.message;await refresh()}catch(e){alert(e.message)}}
async function route(id){try{const d=await api('/api/route/'+id,{method:'POST',body:'{}'});alert('Маршрут перестроен: '+d.mode);await refresh()}catch(e){alert(e.message)}}
function openModal(){$('modal').classList.remove('hidden')}function closeModal(){$('modal').classList.add('hidden')}
async function addShipment(){try{const d=await api('/api/shipments',{method:'POST',body:JSON.stringify({cargo:$('cargo').value,origin:$('origin').value,destination:$('destination').value,mode:$('mode').value,weight:$('weight').value,distance:$('distance').value})});$('addMessage').textContent='Создан груз '+d.shipment.id;await refresh();setTimeout(closeModal,900)}catch(e){$('addMessage').textContent=e.message}}
refresh().catch(e=>alert('Не удалось загрузить данные: '+e.message));
