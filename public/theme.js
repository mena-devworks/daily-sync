/* Day/night theme: saved choice, else the device setting. Loaded in <head> so there's no flash. */
(function(){
  var d=document.documentElement,mq=window.matchMedia&&matchMedia('(prefers-color-scheme: light)');
  function saved(){try{return localStorage.getItem('theme')}catch(e){return null}}
  var SUN='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/></svg>',
      MOON='<svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.5 14.6A8.5 8.5 0 0 1 9.4 3.5a8.5 8.5 0 1 0 11.1 11.1z"/></svg>';
  function set(t){d.setAttribute('data-theme',t);
    var m=document.querySelector('meta[name=theme-color]');if(m)m.content=t==='light'?'#f7f9fe':'#0b1130';
    var b=document.querySelectorAll('button.thm');for(var i=0;i<b.length;i++){
      b[i].innerHTML=t==='light'?MOON:SUN;var ar=d.lang==='ar';
      b[i].title=b[i].ariaLabel=t==='light'?(ar?'الوضع الليلي':'Night mode'):(ar?'الوضع النهاري':'Day mode');}}
  set(saved()||(mq&&mq.matches?'light':'dark'));
  if(mq&&mq.addEventListener)mq.addEventListener('change',function(e){if(!saved())set(e.matches?'light':'dark')});
  window.toggleTheme=function(){var t=d.getAttribute('data-theme')==='light'?'dark':'light';try{localStorage.setItem('theme',t)}catch(e){}set(t)};
  document.addEventListener('DOMContentLoaded',function(){set(d.getAttribute('data-theme'))});
  document.addEventListener('click',function(e){var b=e.target.closest&&e.target.closest('button.thm');if(b){e.preventDefault();toggleTheme()}});
  new MutationObserver(function(){set(d.getAttribute('data-theme'))}).observe(d,{attributes:true,attributeFilter:['lang']});
})();
