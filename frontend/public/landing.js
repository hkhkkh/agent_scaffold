/**
 * Small, dependency-free motion layer for the public product page.
 *
 * The page is still fully readable if this file fails: the `.js` class is the
 * only thing that enables the reveal styles, so no content is left hidden.
 */
(() => {
  document.documentElement.classList.add('js');

  const reduced = () =>
    window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const onReady = () => {
    const prefersReduced = reduced();

    // Scroll reveal -----------------------------------------------------
    const targets = Array.from(
      document.querySelectorAll(
        '.section-head, .feature-card, .template-card, .use-card, .split-copy, ' +
          '.dark-card, .selection-mock, .security-panel, .cta-panel, .trust-inner, ' +
          '.faq details',
      ),
    );

    targets.forEach((element) => {
      element.classList.add('reveal');
      const siblings = Array.from(element.parentElement?.children || []);
      const index = Math.max(0, siblings.indexOf(element));
      element.style.setProperty('--reveal-delay', `${Math.min(index, 6) * 55}ms`);
    });

    if (prefersReduced || !('IntersectionObserver' in window)) {
      targets.forEach((element) => element.classList.add('is-visible'));
    } else {
      const observer = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (!entry.isIntersecting) return;
            entry.target.classList.add('is-visible');
            observer.unobserve(entry.target);
          });
        },
        { rootMargin: '0px 0px -8% 0px', threshold: 0.08 },
      );
      targets.forEach((element) => observer.observe(element));
    }

    // Header state + progress bar --------------------------------------
    const header = document.querySelector('.site-header');
    const progress = document.querySelector('.scroll-progress');
    let ticking = false;

    const updateScroll = () => {
      const y = window.scrollY || document.documentElement.scrollTop;
      header?.classList.toggle('is-scrolled', y > 12);
      const max = document.documentElement.scrollHeight - window.innerHeight;
      if (progress && max > 0) {
        progress.style.transform = `scaleX(${Math.min(1, Math.max(0, y / max))})`;
      }
      ticking = false;
    };

    const onScroll = () => {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(updateScroll);
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll, { passive: true });
    updateScroll();

    // Hero parallax ----------------------------------------------------
    const visual = document.querySelector('.hero-visual');
    const windowEl = visual?.querySelector('.product-window');
    if (visual && windowEl && !prefersReduced && window.matchMedia('(pointer: fine)').matches) {
      let frame = 0;
      visual.addEventListener('pointermove', (event) => {
        if (frame) cancelAnimationFrame(frame);
        frame = requestAnimationFrame(() => {
          const rect = visual.getBoundingClientRect();
          const x = (event.clientX - rect.left) / rect.width - 0.5;
          const y = (event.clientY - rect.top) / rect.height - 0.5;
          windowEl.style.transform =
            `perspective(1400px) rotateY(${-3 + x * 3.2}deg) rotateX(${1.2 - y * 2.4}deg)`;
        });
      });
      visual.addEventListener('pointerleave', () => {
        windowEl.style.transform = '';
      });
    }

    // Rotating hero prompt ---------------------------------------------
    const prompt = document.querySelector('.prompt-text');
    const phrases = [
      '描述你的工作流，AI 来搭第一版…',
      '搭一个竞品调研 Agent…',
      '把客服售后流程自动化…',
      '生成一条联网采集 + 写库流水线…',
    ];
    if (prompt && !prefersReduced) {
      let index = 0;
      prompt.style.transition = 'opacity .22s ease';
      window.setInterval(() => {
        index = (index + 1) % phrases.length;
        prompt.style.opacity = '0';
        window.setTimeout(() => {
          prompt.textContent = phrases[index];
          prompt.style.opacity = '1';
        }, 230);
      }, 4600);
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', onReady, { once: true });
  } else {
    onReady();
  }
})();
