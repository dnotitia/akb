<#-- AKB page frame for every Keycloak login page.
     Replaces keycloak.v2/login/template.ftl. The upstream file's scripts are
     kept as they are (authChecker, passwordVisibility, data-once-link, the
     rfc4648 import map, the Firefox workaround); only the frame around the
     nested sections changes. upstream/keycloak-<version>.json records the
     upstream file this was taken from, so a Keycloak upgrade that changes it
     fails the theme checks until someone reconciles the two.

     One deliberate difference: upstream's inline darkMode script follows the
     system only. js/akb-theme-init.js sets the same kcDarkModeClass, but from
     the person's own choice when they made one. -->
<#import "field.ftl" as field>
<#import "footer.ftl" as loginFooter>
<#assign akbAssetVersion = properties.assetVersion!"dev">
<#assign akbNativeOnly = (client?? && properties.akbNativeOnlyClientAttribute?? && ((client.attributes[properties.akbNativeOnlyClientAttribute])!"") == "true")>

<#macro icon name>
  <svg class="akb-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
    <#switch name>
      <#case "database"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/><#break>
      <#case "layers"><path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/><#break>
      <#case "git-branch"><line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/><#break>
      <#case "sun"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/><#break>
      <#case "moon"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/><#break>
      <#case "monitor"><rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/><#break>
      <#case "globe"><circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/><#break>
      <#case "error"><circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/><#break>
      <#case "success"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/><#break>
      <#case "warning"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/><#break>
      <#case "info"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/><#break>
      <#case "arrow-right"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/><#break>
      <#case "rotate"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><#break>
    </#switch>
  </svg>
</#macro>

<#macro logo size="md">
  <span class="akb-logo akb-logo--${size}">
    <span class="akb-logo__mark" aria-hidden="true">a<span class="akb-logo__spark"></span></span>
    <span class="akb-logo__text">
      <span class="akb-logo__wordmark">akb</span>
      <span class="akb-logo__subtitle">${msg("akbBrandSubtitle")}</span>
    </span>
  </span>
</#macro>

<#macro alert type text>
  <#assign akbAlertType = (type == "error")?then("error", type)>
  <div class="akb-alert akb-alert--${akbAlertType}" <#if type == "error">role="alert"<#else>role="status"</#if>>
    <@icon name=akbAlertType/>
    <span class="akb-alert__text kc-feedback-text">${text}</span>
  </div>
</#macro>

<#macro username>
  <#assign label>
    <#if !realm.loginWithEmailAllowed>${msg("username")}<#elseif !realm.registrationEmailAsUsername>${msg("usernameOrEmail")}<#else>${msg("email")}</#if>
  </#assign>
  <@field.group name="username" label=label>
    <div class="${properties.kcInputGroup}">
      <div class="${properties.kcInputGroupItemClass} ${properties.kcFill}">
        <span class="${properties.kcInputClass} ${properties.kcFormReadOnlyClass}">
          <input id="kc-attempted-username" value="${auth.attemptedUsername}" readonly>
        </span>
      </div>
      <div class="${properties.kcInputGroupItemClass}">
        <button id="reset-login" class="${properties.kcFormPasswordVisibilityButtonClass} kc-login-tooltip" type="button"
              aria-label="${msg('restartLoginTooltip')}" onclick="location.href='${url.loginRestartFlowUrl}'">
            <@icon name="rotate"/>
            <span class="kc-tooltip-text">${msg("restartLoginTooltip")}</span>
        </button>
      </div>
    </div>
  </@field.group>
</#macro>

<#macro registrationLayout bodyClass="" displayInfo=false displayMessage=true displayRequiredFields=false>
<!DOCTYPE html>
<html class="${properties.kcHtmlClass!}" lang="${lang}"<#if realm.internationalizationEnabled> dir="${(locale.rtl)?then('rtl','ltr')}"</#if> data-dark-mode-class="${properties.kcDarkModeClass!}">

<head>
    <meta charset="utf-8">
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
    <meta name="color-scheme" content="light${darkMode?then(' dark', '')}">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="noindex, nofollow">

    <#if properties.meta?has_content>
        <#list properties.meta?split(' ') as meta>
            <meta name="${meta?split('==')[0]}" content="${meta?split('==')[1]}"/>
        </#list>
    </#if>
    <title>${title!}</title>
    <link rel="icon" type="image/svg+xml" href="${url.resourcesPath}/img/favicon.svg?v=${akbAssetVersion}" />
    <#-- Before any stylesheet: the first paint already has the right theme. -->
    <script src="${url.resourcesPath}/${properties.themeInitScript}?v=${akbAssetVersion}"></script>
    <#if properties.stylesCommon?has_content>
        <#list properties.stylesCommon?split(' ') as style>
            <link href="${url.resourcesCommonPath}/${style}" rel="stylesheet" />
        </#list>
    </#if>
    <#if properties.styles?has_content>
        <#list properties.styles?split(' ') as style>
            <link href="${url.resourcesPath}/${style}?v=${akbAssetVersion}" rel="stylesheet" />
        </#list>
    </#if>
    <script type="importmap">
        {
            "imports": {
                "rfc4648": "${url.resourcesCommonPath}/vendor/rfc4648/rfc4648.js"
            }
        }
    </script>
    <#if properties.scripts?has_content>
        <#list properties.scripts?split(' ') as script>
            <script src="${url.resourcesPath}/${script}?v=${akbAssetVersion}" type="text/javascript" defer></script>
        </#list>
    </#if>
    <#if scripts??>
        <#list scripts as script>
            <script src="${script}" type="text/javascript"></script>
        </#list>
    </#if>
    <script type="module" src="${url.resourcesPath}/js/passwordVisibility.js"></script>
    <script type="module">
        <#outputformat "JavaScript">
        import { startSessionPolling } from ${(url.resourcesPath + "/js/authChecker.js")?c};

        startSessionPolling(
            ${url.ssoLoginInOtherTabsUrl?c}
        );
        </#outputformat>
    </script>
    <script type="module">
        document.addEventListener("click", (event) => {
            const link = event.target.closest("a[data-once-link]");

            if (!link) {
                return;
            }

            if (link.getAttribute("aria-disabled") === "true") {
                event.preventDefault();
                return;
            }

            const { disabledClass } = link.dataset;

            if (disabledClass) {
                link.classList.add(...disabledClass.trim().split(/\s+/));
            }

            link.setAttribute("role", "link");
            link.setAttribute("aria-disabled", "true");
        });
    </script>
    <#if authenticationSession??>
        <script type="module">
             <#outputformat "JavaScript">
            import { checkAuthSession } from ${(url.resourcesPath + "/js/authChecker.js")?c};

            checkAuthSession(
                ${authenticationSession.authSessionIdHash?c}
            );
            </#outputformat>
        </script>
    </#if>
    <script>
      // Workaround for https://bugzilla.mozilla.org/show_bug.cgi?id=1404468
      const isFirefox = true;
    </script>
</head>

<body id="keycloak-bg" class="akb-page ${properties.kcBodyClass!} ${bodyClass}" data-page-id="login-${pageId}">
<div class="akb-shell">
  <div class="akb-toolbar">
    <#if realm.internationalizationEnabled && locale?? && locale.supported?size gt 1>
      <label class="akb-locale">
        <@icon name="globe"/>
        <span class="akb-visually-hidden">${msg("languages")}</span>
        <select id="login-select-toggle" class="akb-locale__select" onchange="if (this.value) window.location.href=this.value">
          <#list locale.supported?sort_by("label") as l>
            <option value="${l.url}" ${(l.languageTag == locale.currentLanguageTag)?then('selected','')}>${l.label}</option>
          </#list>
        </select>
      </label>
    </#if>
    <button type="button" id="akb-theme-toggle" class="akb-icon-button akb-theme-toggle"
            data-label-system="${msg('akbThemeSystem')}" data-label-light="${msg('akbThemeLight')}" data-label-dark="${msg('akbThemeDark')}"
            data-label-template="${msg('akbThemeToggleLabel')}"
            aria-label="${msg('akbThemeToggleLabel', msg('akbThemeSystem'))}">
      <span class="akb-theme-toggle__icon akb-theme-toggle__icon--system"><@icon name="monitor"/></span>
      <span class="akb-theme-toggle__icon akb-theme-toggle__icon--light"><@icon name="sun"/></span>
      <span class="akb-theme-toggle__icon akb-theme-toggle__icon--dark"><@icon name="moon"/></span>
    </button>
  </div>

  <main class="akb-layout">
    <section class="akb-brand" aria-label="${msg('akbBrandSubtitle')}">
      <@logo size="lg"/>
      <p class="akb-brand__headline">${msg("akbBrandHeadline")}</p>
      <p class="akb-brand__lead">${msg("akbBrandLead")}</p>
      <ul class="akb-brand__features">
        <li><span class="akb-feature-tile akb-feature-tile--knowledge"><@icon name="database"/></span>${msg("akbBrandFeatureSearch")}</li>
        <li><span class="akb-feature-tile akb-feature-tile--memory"><@icon name="layers"/></span>${msg("akbBrandFeatureVault")}</li>
        <li><span class="akb-feature-tile akb-feature-tile--agent"><@icon name="git-branch"/></span>${msg("akbBrandFeatureAgents")}</li>
      </ul>
    </section>

    <div class="akb-column">
      <div class="akb-column__logo"><@logo size="md"/></div>
      <section class="akb-card ${properties.kcLoginMain!}" aria-labelledby="kc-page-title">
        <h1 class="akb-card__title" id="kc-page-title"><#nested "header"></h1>

        <#if !(auth?has_content && auth.showUsername() && !auth.showResetCredentials())>
            <#if displayRequiredFields>
                <p class="akb-card__required"><span class="${properties.kcInputRequiredClass!}">*</span> ${msg("requiredFields")}</p>
            </#if>
        <#else>
            <#if displayRequiredFields>
                <p class="akb-card__required"><span class="${properties.kcInputRequiredClass!}">*</span> ${msg("requiredFields")}</p>
            </#if>
            <div class="${properties.kcFormClass} ${properties.kcContentWrapperClass} akb-card__username">
              <#nested "show-username">
              <@username />
            </div>
        </#if>

        <#-- App-initiated actions should not see warning messages about the need to complete the action -->
        <#-- during login.                                                                               -->
        <#if displayMessage && message?has_content && (message.type != 'warning' || !isAppInitiatedAction??)>
            <@alert type=message.type text=message.summary/>
        </#if>

        <div class="akb-card__body">
          <#nested "form">
        </div>

        <#if auth?has_content && auth.showTryAnotherWayLink()>
          <form id="kc-select-try-another-way-form" action="${url.loginAction}" method="post" novalidate="novalidate">
              <input type="hidden" name="tryAnotherWay" value="on"/>
              <a id="try-another-way" href="javascript:document.forms['kc-select-try-another-way-form'].requestSubmit()"
                  class="akb-button akb-button--outline akb-button--block akb-card__aside-action">
                    ${msg("doTryAnotherWay")}
              </a>
          </form>
        </#if>

        <#if switchOrganizationEnabled?? && switchOrganizationEnabled>
          <form id="kc-switch-organization-form" action="${url.loginAction}" method="post" novalidate="novalidate">
              <input type="hidden" name="switchOrganization" value="true"/>
              <a id="switch-organization" href="javascript:document.forms['kc-switch-organization-form'].requestSubmit()"
                  class="akb-button akb-button--outline akb-button--block akb-card__aside-action">
                    ${msg("doSwitchOrganization")}
              </a>
          </form>
        </#if>

        <#if !akbNativeOnly>
          <#nested "socialProviders">
        </#if>

        <#if displayInfo>
          <div id="kc-info" class="akb-card__info">
            <div id="kc-info-wrapper">
              <#nested "info">
            </div>
          </div>
        </#if>
      </section>
      <footer class="akb-footer">
        <@loginFooter.content/>
        <span>${msg("akbFooterAttribution")}</span>
      </footer>
    </div>
  </main>
</div>
</body>
</html>
</#macro>
