'use strict';
const $ = id => document.getElementById(id);
let token = new URLSearchParams(location.hash.slice(1)).get('setup');
history.replaceState(null, '', location.pathname);
let current = null, activeStep = 1, renderedSelection = false, answerState = '', timer, localErrorState = null;
const messages = {
  ready: 'Настройка проходит здесь, в вашем браузере.',
  installing: 'Готовим окружение MCP. Первая загрузка может занять несколько минут.',
  installed: 'Окружение и выбранные возможности готовы. Перейдите ко входу.',
  connecting: 'Ожидаем ответ Telegram…',
  qr: 'Сканируйте свежий QR из раздела «Устройства» в Telegram.',
  phone: 'Введите телефон вашего личного аккаунта.',
  code: 'Введите код, который прислал Telegram.',
  password: 'Telegram запросил пароль двухэтапной защиты.',
  authorized: 'Вход сохранён. Осталось проверить подключение MCP.',
  checking: 'Выполняем настоящий вызов MCP…',
  verified: 'Подключение работает. MCP получил ответ от вашего аккаунта.',
  closed: 'Мастер закрыт. Эту вкладку можно закрыть.'
};
const errors = {
  prerequisites_missing: 'Для установки нужны Git и uv. Попросите устанавливающего агента подготовить их, затем запустите этот мастер снова.',
  dependencies_missing: 'Сначала подготовьте MCP на шаге «Возможности».',
  busy_or_cooldown: 'Операция уже идёт или действует ограничение Telegram. Дождитесь её завершения.',
  installation_failed: 'Не удалось подготовить окружение. Попросите агента проверить Git, uv и доступ к сети. Ваш вход не удалялся.',
  mcp_check_failed: 'MCP пока не ответил. Попросите агента проверить запуск и сеть; новая авторизация может не понадобиться.',
  session_busy_or_account_mismatch: 'Сессия занята работающим MCP или не совпала с ожидаемым владельцем. Сначала проверьте существующее подключение на шаге 3.',
  login_expired: 'Telegram закрыл попытку входа. Нажмите «Войти по QR» для нового кода или выберите вход по телефону.',
  code_invalid: 'Код не подошёл. Запустите вход снова и используйте свежий код Telegram.',
  code_expired: 'Срок кода истёк. Запустите вход снова.',
  password_invalid: 'Пароль двухэтапной защиты не подошёл.',
  password_attempts_exhausted: 'После трёх попыток вход остановлен. Проверьте пароль двухэтапной защиты в Telegram и попробуйте позднее.',
  flood_wait: 'Telegram ограничил частоту входов. Дождитесь указанного времени.',
  input_closed: 'Время ожидания ввода истекло. Начните вход снова.',
  invalid_input: 'Проверьте формат введённых данных. Ключи приложения находятся на my.telegram.org/apps.',
  connection_failed: 'Не удалось связаться с Telegram. Проверьте сеть; сессию удалять не нужно.',
  check_input_or_active_service: 'Проверьте выбранные возможности. Если MCP уже работает, сначала используйте проверку на шаге 3.',
  setup_session_required: 'Откройте мастер заново через его ярлык. Ключ доступа к этой странице действует только до закрытия мастера.',
  operation_failed: 'Операция не завершилась. Проверьте существующее подключение или попросите устанавливающего агента помочь.'
};
function notice(text, kind='') { $('notice').textContent = text; $('notice').className = 'notice ' + kind; }
function step(number) {
  activeStep = number;
  for (let i=1; i<=3; i++) $('step-'+i).hidden = number !== i;
  document.querySelectorAll('.step').forEach(b => b.classList.toggle('active', Number(b.dataset.step) === number));
}
async function api(path, data) {
  if (!token) throw new Error('setup_session_required');
  const options = {headers: {Authorization:'Bearer '+token}, cache:'no-store'};
  if (data !== undefined) { options.method='POST'; options.headers['Content-Type']='application/json'; options.body=JSON.stringify(data); }
  const response = await fetch('/api/'+path, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'operation_failed');
  return value;
}
function localError(text) { localErrorState=current?.state || 'ready'; notice(text, 'error'); }
function showError(error) { localError(errors[error.message] || errors.operation_failed); }
function engineVisibility() { $('engine-row').hidden = !document.querySelector('[data-cap="transcribe"]')?.checked; }
function selection(status) {
  $('capabilities').replaceChildren();
  const enabled = status.selection?.enabled || ['read'];
  for (const item of status.capabilities) {
    const label = document.createElement('label'); label.className='cap';
    const input = document.createElement('input'); input.type='checkbox'; input.dataset.cap=item.id;
    input.checked=enabled.includes(item.id); input.disabled=item.id==='read'; input.addEventListener('change', engineVisibility);
    const span=document.createElement('span'), title=document.createElement('strong'), detail=document.createElement('small');
    title.textContent=item.title; detail.textContent=item.detail; span.append(title,detail); label.append(input,span); $('capabilities').append(label);
  }
  $('engine').value=status.selection?.transcription_engine || '';
  engineVisibility(); renderedSelection=true;
  step(status.dependencies_ready && status.selection ? 2 : 1);
}
function render(status) {
  const previous=current?.state, previousInvalid=current?.invalid; current=status;
  if (previous!==status.state) localErrorState=null;
  if (!renderedSelection) selection(status);
  $('badge').textContent = status.reader_only ? 'Общий сервис · только чтение' : 'Локальное подключение';
  $('api-fields').hidden=status.api_configured;
  $('api-ready').hidden=!status.api_configured;
  $('prerequisites').textContent=(!status.git_available || !status.uv_available)
    ? 'Для первой установки агенту нужно подготовить Git и uv. Дальнейший вход проходит в этом окне.' : '';
  $('install').disabled=status.busy;
  $('login-qr').disabled=$('login-phone').disabled=status.busy || status.retry_after_seconds>0 || !status.dependencies_ready || !status.selection || ['authorized','verified'].includes(status.state);
  $('check').disabled=status.busy || !status.dependencies_ready;
  $('qr-panel').hidden=status.state!=='qr';
  if (status.state==='qr') $('qr-image').src='data:image/png;base64,'+status.png;
  else $('qr-image').removeAttribute('src');
  const secretFields={phone:['Телефон с кодом страны','Например, +7…','phone'],code:['Код из Telegram','Код может прийти в уже открытый Telegram.','code'],password:['Пароль двухэтапной защиты','Это пароль Telegram, а не код блокировки телефона. Он не сохраняется.','password']};
  const field=secretFields[status.state]; $('secret-form').hidden=!field;
  if (field) {
    $('secret-label').textContent=field[0]; $('secret-help').textContent=status.invalid?'Пароль не подошёл. Попробуйте ещё раз.':field[1];
    $('secret').type=status.state==='phone'?'tel':'password';
    $('secret').inputMode=status.state==='password'?'text':status.state==='phone'?'tel':'numeric';
    if (answerState!==status.state || status.invalid!==previousInvalid) { $('secret').value=''; if (activeStep===2) $('secret').focus(); }
  }
  answerState=status.state;
  $('login-done').hidden=!['authorized','verified'].includes(status.state);
  $('complete').hidden=status.state!=='verified';
  $('check-result').textContent=status.state==='verified'?'✓ Инициализация MCP и чтение аккаунта прошли.':status.state==='checking'?'Проверяем…':'После входа нажмите кнопку ниже.';
  if (previous!==status.state) {
    if (status.state==='installed') step(2);
    if (['qr','phone','code','password'].includes(status.state)) step(2);
    if (status.state==='authorized') step(3);
    if (status.state==='verified') step(3);
  }
  const wait=status.retry_after_seconds?` Подождите ещё ${status.retry_after_seconds} сек.`:'';
  if (localErrorState!==status.state) notice(status.state==='error'?(errors[status.code]||errors.operation_failed)+wait:messages[status.state]||messages.ready,
    status.state==='error'?'error':['authorized','verified'].includes(status.state)?'good':'');
}
async function refresh() { try { render(await api('status')); } catch(error) { showError(error); } }
document.querySelectorAll('.step').forEach(button=>button.addEventListener('click',()=>step(Number(button.dataset.step))));
$('permissions-form').addEventListener('submit',async event=>{
  event.preventDefault();
  localErrorState=null;
  const enabled=Array.from(document.querySelectorAll('[data-cap]:checked')).map(x=>x.dataset.cap);
  const transcription_engine=enabled.includes('transcribe')?$('engine').value:null;
  if (enabled.includes('transcribe')&&!transcription_engine) { localError('Выберите способ расшифровки голосовых.'); return; }
  try { await api('install',{enabled,transcription_engine}); await refresh(); } catch(error) { showError(error); }
});
async function login(method) {
  localErrorState=null;
  const data={method};
  if (!current?.api_configured) { data.api_id=$('api-id').value.trim(); data.api_hash=$('api-hash').value.trim(); }
  if (!current?.api_configured && (!/^[1-9][0-9]*$/.test(data.api_id) || !/^[a-f0-9]{32}$/i.test(data.api_hash))) {
    localError('Заполните API ID и API Hash из кабинета Telegram, затем выберите способ входа.'); return;
  }
  try { await api('login',data); $('api-id').value=$('api-hash').value=''; await refresh(); } catch(error) { showError(error); }
}
$('login-qr').addEventListener('click',()=>login('qr'));
$('login-phone').addEventListener('click',()=>login('phone'));
$('secret-form').addEventListener('submit',async event=>{
  event.preventDefault(); const field={phone:'phone',code:'code',password:'password'}[current?.state]; if (!field) return;
  const data={[field]:$('secret').value}; $('secret').value='';
  try { await api('answer',data); await refresh(); } catch(error) { showError(error); }
});
$('check').addEventListener('click',async()=>{try { await api('check',{}); await refresh(); } catch(error) { showError(error); }});
$('close').addEventListener('click',async()=>{try { await api('close',{}); clearInterval(timer); token=null; $('qr-image').removeAttribute('src'); $('secret').value=''; notice(messages.closed); document.querySelectorAll('button,input,select').forEach(x=>x.disabled=true); } catch(error) { showError(error); }});
step(1); refresh(); timer=setInterval(refresh,1200);
