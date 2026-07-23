/* =============================================================
   static/orb.js — Orbe de carga animado (canvas 2D, sin dependencias)
   =============================================================
   Indicador de "pensando/cargando" con estética de globo punteado, en la
   línea de los "thinking orbs" de las UIs de agentes, pero escrito a mano
   en JavaScript plano para encajar con este proyecto (Flask + Jinja + JS
   sin build ni React).

   Uso declarativo — cualquier <canvas> con data-orb se anima solo:

     <canvas data-orb data-orb-size="92" data-orb-state="searching"></canvas>
     <canvas data-orb data-orb-size="40" data-orb-state="working"></canvas>

   Características:
   - Monocromo: usa la tinta del tema (--text-main), así que se invierte
     solo en modo oscuro. Se actualiza en vivo con el evento
     "sonarthemechange" que dispara el toggle de tema.
   - Todas las instancias comparten un mismo reloj (un solo requestAnimationFrame).
   - Se pausa cuando la pestaña está oculta o el orbe no está en pantalla
     (IntersectionObserver), y reanuda en fase.
   - Respeta prefers-reduced-motion: dibuja un frame estático.
   - Solo canvas 2D (arcos): nada de WebGL ni filtros, barato y consistente
     entre navegadores. DPR limitado a 2.
   ============================================================= */
(function () {
  "use strict";

  var TAU = Math.PI * 2;
  var reduceMotion =
    window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var orbs = [];       // instancias activas
  var rafId = null;
  var startTime = 0;
  var inkRGB = "29, 29, 31"; // fallback (--text-main claro); se recalcula

  // ---- Color de tinta según el tema -------------------------------------
  function hexToRgb(hex) {
    hex = (hex || "").trim().replace("#", "");
    if (hex.length === 3) {
      hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
    }
    if (hex.length !== 6) return null;
    var n = parseInt(hex, 16);
    if (isNaN(n)) return null;
    return (n >> 16) + ", " + ((n >> 8) & 255) + ", " + (n & 255);
  }

  function refreshInk() {
    var raw = getComputedStyle(document.documentElement)
      .getPropertyValue("--text-main");
    var rgb = hexToRgb(raw);
    if (rgb) inkRGB = rgb;
  }

  // ---- Puntos distribuidos en una esfera (espiral de Fibonacci) ---------
  // Da un reparto parejo, sin polos amontonados como una malla lat/long.
  function fibonacciSphere(count) {
    var pts = [];
    var golden = Math.PI * (3 - Math.sqrt(5));
    for (var i = 0; i < count; i++) {
      var y = 1 - (i / (count - 1)) * 2; // 1 .. -1
      var radius = Math.sqrt(1 - y * y);
      var theta = golden * i;
      pts.push({
        x: Math.cos(theta) * radius,
        y: y,
        z: Math.sin(theta) * radius,
      });
    }
    return pts;
  }

  function makeOrb(canvas) {
    var size = parseInt(canvas.getAttribute("data-orb-size"), 10) || 64;
    var state = canvas.getAttribute("data-orb-state") || "searching";
    var speed = parseFloat(canvas.getAttribute("data-orb-speed")) || 1;
    var dpr = Math.min(window.devicePixelRatio || 1, 2);

    canvas.width = size * dpr;
    canvas.height = size * dpr;
    canvas.style.width = size + "px";
    canvas.style.height = size + "px";
    if (!canvas.getAttribute("role")) canvas.setAttribute("role", "img");

    var ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    // La cantidad de puntos escala con el tamaño: un orbe chico con 110
    // puntos se ve como una mancha.
    var count = Math.round(Math.max(26, Math.min(130, size * 1.25)));

    return {
      canvas: canvas,
      ctx: ctx,
      size: size,
      state: state,
      speed: speed,
      points: fibonacciSphere(count),
      visible: true,
    };
  }

  // ---- Dibujo de un orbe en un instante t (segundos) --------------------
  function drawOrb(orb, t) {
    var ctx = orb.ctx;
    var size = orb.size;
    var cx = size / 2;
    var cy = size / 2;
    var R = size * 0.4;
    var baseDot = size * 0.013 + 0.6;

    ctx.clearRect(0, 0, size, size);

    // Rotación del globo sobre su eje vertical + una leve inclinación fija
    // para que se vea la profundidad.
    var ay = t * 0.6 * orb.speed;
    var tilt = 0.42;
    var cosY = Math.cos(ay), sinY = Math.sin(ay);
    var cosT = Math.cos(tilt), sinT = Math.sin(tilt);

    // "searching": una banda de barrido vertical recorre el globo.
    // "working": sin barrido, solo el globo girando con un pulso suave.
    var sweepX = null;
    if (orb.state === "searching") {
      sweepX = Math.sin(t * 1.15 * orb.speed) * R * 0.95;
    }
    var pulse = orb.state === "working"
      ? 0.85 + Math.sin(t * 2.4 * orb.speed) * 0.15
      : 1;

    var pts = orb.points;
    for (var i = 0; i < pts.length; i++) {
      var p = pts[i];
      // Rotar en Y
      var x1 = p.x * cosY + p.z * sinY;
      var z1 = -p.x * sinY + p.z * cosY;
      var y1 = p.y;
      // Inclinar en X
      var y2 = y1 * cosT - z1 * sinT;
      var z2 = y1 * sinT + z1 * cosT;

      var sx = cx + x1 * R;
      var sy = cy + y2 * R;
      var depth = (z2 + 1) / 2; // 0 (atrás) .. 1 (adelante)

      var r = baseDot * (0.45 + depth * 0.85) * pulse;
      var alpha = (0.12 + depth * 0.5) * pulse;

      // Barrido: los puntos del frente cerca de la línea de barrido se
      // encienden, dando el efecto de escaneo.
      if (sweepX !== null && depth > 0.5) {
        var d = Math.abs(sx - (cx + sweepX));
        var near = Math.max(0, 1 - d / (R * 0.4));
        if (near > 0) {
          alpha += near * 0.45;
          r += near * baseDot * 0.9;
        }
      }

      if (alpha <= 0.02) continue;
      ctx.beginPath();
      ctx.fillStyle = "rgba(" + inkRGB + "," + Math.min(alpha, 1).toFixed(3) + ")";
      ctx.arc(sx, sy, r, 0, TAU);
      ctx.fill();
    }
  }

  // ---- Bucle de animación (un solo reloj para todas las instancias) -----
  function frame(now) {
    if (!startTime) startTime = now;
    var t = (now - startTime) / 1000;
    for (var i = 0; i < orbs.length; i++) {
      if (orbs[i].visible) drawOrb(orbs[i], t);
    }
    rafId = requestAnimationFrame(frame);
  }

  function startLoop() {
    if (rafId === null && !reduceMotion) rafId = requestAnimationFrame(frame);
  }
  function stopLoop() {
    if (rafId !== null) {
      cancelAnimationFrame(rafId);
      rafId = null;
      startTime = 0;
    }
  }

  function anyVisible() {
    for (var i = 0; i < orbs.length; i++) if (orbs[i].visible) return true;
    return false;
  }

  function syncLoop() {
    if (document.hidden || !anyVisible()) stopLoop();
    else startLoop();
  }

  // ---- Inicialización ---------------------------------------------------
  function init() {
    refreshInk();
    var canvases = document.querySelectorAll("canvas[data-orb]");
    if (!canvases.length) return;

    canvases.forEach(function (canvas) {
      var orb = makeOrb(canvas);
      orbs.push(orb);

      if (reduceMotion) {
        // Frame estático representativo.
        drawOrb(orb, 0.6);
      }
    });

    if (reduceMotion) return;

    // Pausar los que no están en pantalla.
    if ("IntersectionObserver" in window) {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          for (var i = 0; i < orbs.length; i++) {
            if (orbs[i].canvas === e.target) orbs[i].visible = e.isIntersecting;
          }
        });
        syncLoop();
      });
      orbs.forEach(function (o) { io.observe(o.canvas); });
    }

    document.addEventListener("visibilitychange", syncLoop);
    window.addEventListener("sonarthemechange", refreshInk);
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)")
        .addEventListener("change", refreshInk);
    }

    syncLoop();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.AppOrb = { refreshInk: refreshInk };
})();
