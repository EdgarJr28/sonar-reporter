/* =============================================================
   static/tooltip.js — Componente de tooltips reutilizable
   =============================================================
   Muestra el texto completo de aquellos elementos cuyo contenido aparece
   recortado en pantalla (nombres de archivo largos, mensajes de issues,
   claves de proyecto, celdas de tablas, etc.).

   Dos comportamientos, según cómo se marque el elemento:

   1) Automático por recorte — cualquier elemento que coincida con
      TRUNCATE_SELECTOR muestra tooltip SOLO si su texto realmente no
      entra (se compara scrollWidth/scrollHeight contra client*). Si el
      texto se ve completo no molesta con un tooltip redundante.

   2) Explícito — cualquier elemento con [data-tip="..."] muestra ese
      texto siempre, entre completo o no.

   Detalles de implementación que importan:

   - El tooltip se renderiza en un único elemento pegado al <body> (patrón
     "portal"), no dentro de la celda. Así no lo recorta el overflow del
     contenedor ni queda tapado por vecinos con otro stacking context —
     que es justamente el problema que tienen los tooltips hechos con
     ::after dentro de una tabla o un grid.
   - Todo se maneja por delegación de eventos en document, así que las
     filas que se agregan después (scroll infinito del modal de detalle,
     re-render de AG Grid) quedan cubiertas sin re-inicializar nada.
   - Accesible por teclado: también aparece con focus, no solo con hover.

   Se carga desde templates/_tooltip.html.
   ============================================================= */
(function () {
  "use strict";

  // Elementos que pueden quedar recortados. Se les mira el tamaño real
  // antes de decidir si mostrar algo.
  var TRUNCATE_SELECTOR = [
    // Celdas de la tabla de issues (AG Grid). AG Grid ya recorta con
    // ellipsis, así que alcanza con mirarlas: se usa el componente propio
    // en vez de tooltipField para que el tooltip se vea igual en toda la
    // app y no con el estilo por defecto de la librería.
    ".ag-cell",
    ".ag-header-cell-text",
    ".metric-modal-file",
    ".metric-modal-rule",
    ".metric-modal-msg",
    ".project-picker-name",
    ".project-picker-meta",
    ".compare-table td",
    ".compare-table th",
    ".insight-list li",
    ".user-menu-name",
    ".user-menu-fullname",
    ".custom-select-label",
    ".pdf-export-toast-message",
    ".truncate", // utilidad genérica, ver style.css
  ].join(",");

  var SHOW_DELAY = 140;   // ms antes de aparecer (evita parpadeo al pasar rápido)
  var EDGE_MARGIN = 8;    // margen mínimo contra el borde de la ventana
  var GAP = 8;            // separación entre el elemento y el tooltip

  var tipEl = null;
  var showTimer = null;
  var currentTarget = null;

  function getTipEl() {
    if (tipEl) return tipEl;
    tipEl = document.createElement("div");
    tipEl.className = "app-tooltip";
    tipEl.setAttribute("role", "tooltip");
    tipEl.hidden = true;
    document.body.appendChild(tipEl);
    return tipEl;
  }

  /* ¿El contenido de este elemento está realmente recortado?
     - scrollWidth > clientWidth  -> recorte horizontal (text-overflow: ellipsis)
     - scrollHeight > clientHeight -> recorte vertical (-webkit-line-clamp)
     Se usa una tolerancia de 1px porque los navegadores redondean y si no
     aparecerían tooltips en textos que en realidad se ven enteros. */
  function isTruncated(el) {
    return (
      el.scrollWidth - el.clientWidth > 1 || el.scrollHeight - el.clientHeight > 1
    );
  }

  /* Texto a mostrar: el explícito de data-tip, o el propio del elemento. */
  function resolveText(el) {
    var explicit = el.getAttribute("data-tip");
    if (explicit) return explicit;
    if (!el.matches(TRUNCATE_SELECTOR)) return null;
    if (!isTruncated(el)) return null;
    var text = (el.textContent || "").trim();
    return text || null;
  }

  /* ¿Este contenido es una ruta / identificador, o texto corriente?
     Las rutas van en una sola línea; el texto conserva sus saltos.

     Se decide primero por el origen (columnas que sabemos que traen rutas
     o claves de regla) y, si no, por la forma del contenido: algo sin
     espacios que tenga barras, puntos o dos puntos es una ruta o un
     identificador tipo "java:S1128", no una frase. */
  var PATH_SELECTOR = ".metric-modal-file, .metric-modal-rule, .project-picker-meta";

  function isPathLike(el, text) {
    if (el.matches(PATH_SELECTOR)) return true;
    if (el.hasAttribute("data-tip-mono")) return true;
    // Sin espacios y con separadores típicos de ruta/identificador.
    return !/\s/.test(text) && /[\/\\.:]/.test(text);
  }

  function position(el) {
    var tip = getTipEl();
    var r = el.getBoundingClientRect();
    var tr = tip.getBoundingClientRect();

    // Por defecto arriba; si no entra, se voltea abajo.
    var top = r.top - tr.height - GAP;
    var placeBelow = top < EDGE_MARGIN;
    if (placeBelow) top = r.bottom + GAP;

    // Centrado horizontal, sin salirse de la ventana.
    var idealLeft = r.left + r.width / 2 - tr.width / 2;
    var left = idealLeft;
    var maxLeft = window.innerWidth - tr.width - EDGE_MARGIN;
    if (left > maxLeft) left = maxLeft;
    if (left < EDGE_MARGIN) left = EDGE_MARGIN;

    tip.style.left = Math.round(left) + "px";
    tip.style.top = Math.round(top) + "px";
    tip.classList.toggle("app-tooltip-below", placeBelow);

    // La puntita debe seguir apuntando al centro del elemento aunque el
    // tooltip se haya corrido para no salirse de la pantalla. Se calcula
    // relativa al tooltip y se deja un margen para que no se monte sobre
    // las esquinas redondeadas.
    var targetCenter = r.left + r.width / 2;
    var arrowX = targetCenter - left;
    var minArrow = 12;
    var maxArrow = tr.width - 12;
    var fits = arrowX >= minArrow && arrowX <= maxArrow;
    tip.classList.toggle("app-tooltip-no-arrow", !fits);
    if (fits) tip.style.setProperty("--arrow-x", Math.round(arrowX) + "px");
  }

  function show(el) {
    var text = resolveText(el);
    if (!text) return;

    var tip = getTipEl();
    tip.textContent = text;
    // Rutas de archivo y claves de regla van en monoespaciada y en una sola
    // línea (ver .app-tooltip-mono en style.css): partir una ruta en varios
    // renglones la hace difícil de seguir. El texto común (mensajes de
    // issues, conclusiones) sí conserva los saltos de línea.
    // Importante: la clase se aplica ANTES de position(), porque cambia el
    // ancho del tooltip y por lo tanto dónde hay que ubicarlo.
    tip.classList.toggle("app-tooltip-mono", isPathLike(el, text));
    tip.hidden = false;
    // Se posiciona después de asignar el texto: recién ahí el tooltip
    // tiene su ancho/alto definitivos para poder centrarlo y voltearlo.
    position(el);
    tip.classList.add("is-visible");
    currentTarget = el;
  }

  function hide() {
    clearTimeout(showTimer);
    showTimer = null;
    currentTarget = null;
    if (!tipEl) return;
    tipEl.classList.remove("is-visible");
    tipEl.hidden = true;
  }

  function scheduleShow(el) {
    clearTimeout(showTimer);
    showTimer = setTimeout(function () {
      show(el);
    }, SHOW_DELAY);
  }

  function findTarget(node) {
    if (!node || node.nodeType !== 1) return null;
    return node.closest("[data-tip]," + TRUNCATE_SELECTOR);
  }

  document.addEventListener("mouseover", function (e) {
    var target = findTarget(e.target);
    if (!target || target === currentTarget) return;
    hide();
    scheduleShow(target);
  });

  document.addEventListener("mouseout", function (e) {
    var target = findTarget(e.target);
    if (!target) return;
    // Si el mouse sigue dentro del mismo elemento (pasó a un hijo), no se oculta.
    if (e.relatedTarget && target.contains(e.relatedTarget)) return;
    hide();
  });

  // Teclado: mismo comportamiento al enfocar/desenfocar.
  document.addEventListener("focusin", function (e) {
    var target = findTarget(e.target);
    if (target) scheduleShow(target);
  });
  document.addEventListener("focusout", hide);

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") hide();
  });

  // Al hacer scroll o redimensionar, el tooltip quedaría "flotando" lejos
  // de su elemento, así que se esconde. Se escucha en fase de captura para
  // enterarse también del scroll de contenedores internos (modal, tablas).
  window.addEventListener("scroll", hide, true);
  window.addEventListener("resize", hide);

  // Expuesto por si en algún momento hace falta cerrarlo desde otro script
  // (por ejemplo al abrir un modal encima).
  window.AppTooltip = { hide: hide };
})();
