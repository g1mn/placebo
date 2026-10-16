(function () {
  function setupToggle(buttonSelector, panelId) {
    var button = document.querySelector(buttonSelector);
    var panel = document.getElementById(panelId);
    if (!button || !panel) {
      return;
    }
    button.addEventListener('click', function () {
      var isHidden = panel.hasAttribute('hidden');
      if (isHidden) {
        panel.removeAttribute('hidden');
        button.setAttribute('aria-expanded', 'true');
      } else {
        panel.setAttribute('hidden', '');
        button.setAttribute('aria-expanded', 'false');
      }
    });
  }

  setupToggle('.search-toggle', 'search-panel');
  setupToggle('.menu-toggle', 'menu-panel');

  var forms = document.querySelectorAll('form');
  for (var i = 0; i < forms.length; i++) {
    forms[i].addEventListener('submit', function (event) {
      event.preventDefault();
      var status = this.querySelector('.form-status');
      if (status) {
        status.textContent = 'Demo store: nothing was sent.';
        status.removeAttribute('hidden');
      }
    });
  }
})();
