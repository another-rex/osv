import './styles.scss';
import '@github/clipboard-copy-element';
import '@github/time-elements';
import '@material/mwc-circular-progress';
import '@material/mwc-icon';
import '@material/mwc-icon-button';
import '@hotwired/turbo';
import 'spicy-sections/src/SpicySections';
import {TextField as MwcTextField} from '@material/mwc-textfield';
import {LitElement, html, css, unsafeCSS} from 'lit';
import {unsafeHTML} from 'lit/directives/unsafe-html.js';
import hljs from 'highlight.js';
// TODO: raw-loader is deprecated.
import hljsStyles from '!!raw-loader!highlight.js/styles/github-dark.css';

import { throttle } from "throttle-debounce";

document.addEventListener('turbo:load', () => {
  const queryField = document.querySelector('.query-field');
  if (queryField) { // If we are in the list page
    const searchForm = document.querySelector('#search-form');
    const queryAutocompleteForm = document.querySelector('#autocomplete-form');
    const searchResultBox = document.querySelector('#search-result-box');
    const queryAutocompleteField = document.querySelector('#query-ac-input');
    const ecosystemAutocompleteField = document.querySelector('#ecosystem-ac-input');

    const throttled_query_submit = throttle(500, () => {
      queryAutocompleteField.value = queryField.value;
      const ecosystemRadio = document.querySelector('input[name=ecosystem]:checked');
      ecosystemAutocompleteField.value = ecosystemRadio.value || "";
      if (queryAutocompleteField.value.length < 2) {
        hideSearchBox();
        return;
      }

      queryAutocompleteForm.requestSubmit();
    });
    
    queryField.addEventListener('input', () => {
      throttled_query_submit();
    });

    searchForm.addEventListener('submit', () => {
      hideSearchBox();
    });

    function hideSearchBox() {
      let box = document.querySelector('.search-result-box-inner');
      if (box) {
        box.classList.add('hidden');
      }
    }

    function selectEcosystem(ecosystem) {
      const ecosystemButtons = document.querySelectorAll('#search-form input[name=ecosystem]');
      for (const elem of ecosystemButtons) {
        if (elem.value == ecosystem) {
          elem.checked = true;
        }
      }
    }

    window.autocompleteClick = function (query, ecosystem) {
      if (queryField) {
        queryField.value = query;
        selectEcosystem(ecosystem);
        searchForm.requestSubmit();
        hideSearchBox();
      }
    }

    const observer = new MutationObserver(() => {
      // Search box result arrived, check if search box still needs to be hidden
      // This can happen if the results for another query arrive after 
      // queryAutocompleteField goes below length 2
      if (queryAutocompleteField.value.length < 2) {
        hideSearchBox();
      }
    });

    observer.observe(searchResultBox, { childList: true });
  }
});


// Submits a form in a way such that Turbo can intercept the event.
// Triggering submit on the form directly would still give a correct resulting
// page, but we want to let Turbo speed up renders as intended.
const submitForm = function(form) {
  if (!form) {
    return;
  }
  // Use request submit instead of submit for turbo to intercept
  form.requestSubmit();
}

// A wrapper around <input type=radio> elements that submits their parent form
// when any radio item changes.
export class SubmitRadiosOnClickContainer extends LitElement {
  constructor() {
    super();
    this.addEventListener('change', () => submitForm(this.closest('form')))
  }
  // Render the contents of the element as-is.
  render() { return html`<slot></slot>`; }
}
customElements.define('submit-radios', SubmitRadiosOnClickContainer);

// A wrapper around <mwc-textfield> that adds back native-like enter key form
// submission behavior.
export class MwcTextFieldWithEnter extends MwcTextField {
  constructor() {
    super();
    this.addEventListener('keyup', (e) => {
      if (e.key === 'Enter') {
        submitForm(this.closest('form'));
      }
    });
  }
}
customElements.define('mwc-textfield-with-enter', MwcTextFieldWithEnter);

export class CodeBlock extends LitElement {
  static get styles() {
    return [
      css`${unsafeCSS(hljsStyles)}`,
      css`:host pre {
        font-family: inherit;
        background: #333;
        border-radius: 10px;
        display: block;
        overflow: auto;
        padding: 10px;
      }`];
  }
  render() {
    const highlighted = hljs.highlightAuto(this.innerHTML).value;
    return html`<pre>${unsafeHTML(highlighted)}</pre>`;
  }
}
customElements.define('code-block', CodeBlock);
