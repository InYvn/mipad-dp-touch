/* Progressive motion: content is never hidden while waiting for JavaScript. */
(() => {
  'use strict';
  const preference = matchMedia('(prefers-reduced-motion: reduce)');
  const running = new Set();
  const played = new WeakSet();
  const enter = (element, delay = 0, image = false) => {
    if (preference.matches || played.has(element) || !element.animate) return;
    played.add(element);
    const animation = element.animate([
      { opacity: 0.18, transform: image ? 'translateY(18px) scale(.985)' : 'translateY(14px)' },
      { opacity: 1, transform: 'none' }
    ], { duration: image ? 760 : 580, delay, easing: 'cubic-bezier(.22,1,.36,1)', fill: 'backwards' });
    running.add(animation);
    animation.finished.then(() => running.delete(animation), () => running.delete(animation));
  };
  const header = document.querySelector('header .wrap');
  if (header) [...header.children].forEach((element, index) => enter(element, Math.min(index * 65, 325)));
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(entries => {
      const visible = entries.filter(entry => entry.isIntersecting);
      visible.forEach((entry, index) => {
        enter(entry.target, Math.min(index * 55, 165), entry.target.matches('.shot'));
        observer.unobserve(entry.target);
      });
    }, { threshold: 0, rootMargin: '0px 0px -32px 0px' });
    document.querySelectorAll('main section h2, main section .kicker, main section .lead, .compare > .col, .feature, .shot, .status-card, .item, .faq-list > details, .steps > li, .cta-row, .callout, .more > p').forEach(element => {
      // Do not nest entrance transforms or alter the tutorial's gesture stages.
      if (!element.closest('.tut, .item, .steps li') || element.matches('.item, .steps > li')) observer.observe(element);
    });
    preference.addEventListener('change', () => {
      if (preference.matches) {
        running.forEach(animation => animation.cancel());
        running.clear();
        observer.disconnect();
      }
    });
  }
  document.querySelectorAll('nav .links a').forEach(link => {
    link.addEventListener('click', () => {
      // Brief local feedback only; navigation and modified clicks stay native.
      link.classList.add('is-following');
      setTimeout(() => link.classList.remove('is-following'), 240);
    });
  });
})();
