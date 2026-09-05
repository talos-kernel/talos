/* Public, local-only interface interactions. No agent connection or telemetry. */
(() => {
  const nav = document.querySelector('nav');
  if (nav) {
    const wrap = nav.querySelector('.wrap');
    const links = [...nav.querySelectorAll('a.lnk')];
    if (wrap && links.length) {
      const group = document.createElement('div');
      group.className = 'nav-links'; group.id = 'site-navigation';
      links.forEach(link => group.append(link));
      const toggle = document.createElement('button');
      toggle.type = 'button'; toggle.className = 'menu-toggle'; toggle.textContent = 'Menu +';
      toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-controls', group.id);
      const close = () => { nav.removeAttribute('data-open'); toggle.setAttribute('aria-expanded','false'); toggle.textContent='Menu +'; };
      toggle.addEventListener('click', () => {
        if (nav.hasAttribute('data-open')) close();
        else { nav.setAttribute('data-open',''); toggle.setAttribute('aria-expanded','true'); toggle.textContent='Close −'; }
      });
      nav.addEventListener('keydown', event => { if (event.key==='Escape') {close();toggle.focus();} });
      links.forEach(link => link.addEventListener('click', close));
      wrap.append(toggle,group); nav.setAttribute('data-menu',''); nav.setAttribute('aria-label','Main navigation');
    }
    const target = document.querySelector('header, main');
    if (target) {
      if (!target.id) target.id='main-content';
      target.tabIndex=-1;
      const skip=document.createElement('a');skip.className='skip-link';skip.href='#'+target.id;skip.textContent='Skip to content';
      document.body.prepend(skip);
    }
  }
  const preview=document.getElementById('mission-preview');
  if (!preview) return;
  const scenarios={
    build:{request:'Fix the failing test. Show me what changed.',state:'TURN FINISHED',meta:'Example · 34s · 4 tool calls',
      steps:[['🔎','Read the failure and its surrounding code.'],['🧑‍💻','Hand a scoped implementation to the worker.'],['🧪','Read back the patch and run the relevant check.'],['✅','Report the checked result and remaining limits.']],
      result:'The patch is ready to review. A worker receipt is followed by a separate check.'},
    approval:{request:'Run this maintenance command on the server.',state:'NEEDS YOUR APPROVAL',meta:'Example · remote effect · waiting for you',
      steps:[['📡','Select an operator-configured host.'],['🛡️','Check the exact host and command.'],['⏸️','Show the action that needs approval.']],
      result:'Nothing remote runs until the operator approves. A chat reply cannot grant itself permission.'},
    failure:{request:'Build the change with the coding worker.',state:'WORKER UNAVAILABLE',meta:'Example · configuration missing · no job started',
      steps:[['🧭','Choose the enabled worker for the task.'],['⚠️','The worker reports missing configuration.'],['🔎','Name the missing prerequisite or another enabled route.']],
      result:'A visible failure with a concrete next step. No invented success, no unconfined fallback.'}
  };
  const request=preview.querySelector('[data-request]'),state=preview.querySelector('[data-state]');
  const meta=preview.querySelector('[data-meta]'),trail=preview.querySelector('[data-trail]'),result=preview.querySelector('[data-result]');
  const play=preview.querySelector('[data-play]');
  const controls=[...document.querySelectorAll('[data-scenario]')];
  let current='build',timer=null;
  const stop=()=>{clearTimeout(timer);timer=null;play.disabled=false;play.textContent='Replay example';};
  function render(count,playing=false){
    const item=scenarios[current];request.textContent=item.request;
    state.textContent=playing?'WORKING':item.state;
    meta.textContent=playing?'Illustrative sequence · not a live agent':item.meta;
    trail.replaceChildren(...item.steps.slice(0,count).map(([icon,text])=>{
      const row=document.createElement('li'),glyph=document.createElement('span'),label=document.createElement('span');
      glyph.textContent=icon;glyph.setAttribute('aria-hidden','true');label.textContent=text;row.append(glyph,label);return row;
    }));
    result.textContent=playing?'Following the example…':item.result;
  }
  controls.forEach(button=>button.addEventListener('click',()=>{
    stop();current=button.dataset.scenario;
    controls.forEach(control=>control.setAttribute('aria-pressed',String(control===button)));
    render(scenarios[current].steps.length);
  }));
  play.addEventListener('click',()=>{
    stop();
    if(matchMedia('(prefers-reduced-motion: reduce)').matches){render(scenarios[current].steps.length);return;}
    play.disabled=true;play.textContent='Playing example…';let count=0;
    function advance(){count++;const finished=count>=scenarios[current].steps.length;render(count,!finished);
      if(finished)stop();else timer=setTimeout(advance,850);}
    advance();
  });
  document.addEventListener('visibilitychange',()=>{if(document.hidden){stop();render(scenarios[current].steps.length);}});
  render(scenarios[current].steps.length);
})();
