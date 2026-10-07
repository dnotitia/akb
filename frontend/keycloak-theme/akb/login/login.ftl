<#-- Sign-in page. Replaces keycloak.v2/login/login.ftl.
     Identity providers come first: on AKB's own paths most people arrive
     through one. The realm's own accounts (the product administrator, and
     people an installation keeps local) follow under their own heading.
     A client marked native-only (AKB's /admin client) gets the password form
     alone: a provider button there would lead to a sign-in /admin refuses. -->
<#import "template.ftl" as layout>
<#import "field.ftl" as field>
<#import "buttons.ftl" as buttons>
<#import "passkeys.ftl" as passkeys>
<#assign akbNativeOnly = (client?? && properties.akbNativeOnlyClientAttribute?? && ((client.attributes[properties.akbNativeOnlyClientAttribute])!"") == "true")>
<#assign akbProviders = (!akbNativeOnly && realm.password && social?? && social.providers?? && social.providers?has_content)>
<@layout.registrationLayout displayMessage=!messagesPerField.existsError('username','password') displayInfo=realm.password && realm.registrationAllowed && !registrationDisabled??; section>
<!-- template: login.ftl (akb) -->

    <#if section = "header">
        <#if akbNativeOnly>${msg("akbAdminLoginTitle")}<#else>${msg("akbLoginTitle")}</#if>
    <#elseif section = "form">
        <p class="akb-card__lead"><#if akbNativeOnly>${msg("akbAdminLoginLead")}<#elseif akbProviders>${msg("akbLoginLead")}<#else>${msg("akbLocalLoginLead")}</#if></p>

        <#if akbProviders>
          <div id="kc-social-providers" class="akb-providers">
            <ul class="akb-providers__list">
              <#list social.providers as p>
                <li>
                  <a data-once-link data-disabled-class="akb-button--busy" id="social-${p.alias}"
                     class="akb-button akb-button--outline akb-button--block akb-button--lg akb-provider"
                     href="${p.loginUrl}">
                    <span class="akb-provider__label">${msg("akbIdentityProviderAction", p.displayName!p.alias)}</span>
                  </a>
                </li>
              </#list>
            </ul>
          </div>
        </#if>

        <div id="kc-form">
          <div id="kc-form-wrapper" class="<#if akbProviders>akb-local-accounts</#if>">
            <#if realm.password>
                <#if akbProviders>
                  <p class="akb-divider"><span>${msg("akbLocalAccountsHeading")}</span></p>
                </#if>
                <form id="kc-form-login" class="${properties.kcFormClass!}" onsubmit="login.disabled = true; return true;" action="${url.loginAction}" method="post" novalidate="novalidate">
                    <#if !usernameHidden??>
                        <#assign label>
                            <#if !realm.loginWithEmailAllowed>${msg("username")}<#elseif !realm.registrationEmailAsUsername>${msg("usernameOrEmail")}<#else>${msg("email")}</#if>
                        </#assign>
                        <@field.input name="username" label=label error=messagesPerField.getFirstError('username','password')
                            autofocus=!akbProviders autocomplete="${(enableWebAuthnConditionalUI?has_content)?then('username webauthn', 'username')}" value=login.username!'' />
                        <@field.password name="password" label=msg("password") error="" forgotPassword=realm.resetPasswordAllowed autofocus=usernameHidden?? autocomplete="current-password">
                            <#if realm.rememberMe && !usernameHidden??>
                                <@field.checkbox name="rememberMe" label=msg("rememberMe") value=login.rememberMe?? />
                            </#if>
                        </@field.password>
                    <#else>
                        <@field.password name="password" label=msg("password") forgotPassword=realm.resetPasswordAllowed autofocus=usernameHidden?? autocomplete="current-password">
                            <#if realm.rememberMe && !usernameHidden??>
                                <@field.checkbox name="rememberMe" label=msg("rememberMe") value=login.rememberMe?? />
                            </#if>
                        </@field.password>
                    </#if>

                    <input type="hidden" id="id-hidden-input" name="credentialId" <#if auth.selectedCredential?has_content>value="${auth.selectedCredential}"</#if>/>
                    <div class="akb-form-actions">
                      <button class="akb-button akb-button--primary akb-button--block akb-button--lg" name="login" id="kc-login" type="submit">
                        <span>${msg("doLogIn")}</span>
                        <@layout.icon name="arrow-right"/>
                      </button>
                    </div>
                </form>
            </#if>
          </div>
        </div>
        <@passkeys.conditionalUIData />
    <#elseif section = "socialProviders" >
        <#-- Drawn above the form, in "form". -->
    <#elseif section = "info" >
        <#if realm.password && realm.registrationAllowed && !registrationDisabled??>
            <div id="kc-registration-container">
                <div id="kc-registration">
                    <span>${msg("noAccount")} <a href="${url.registrationUrl}">${msg("doRegister")}</a></span>
                </div>
            </div>
        </#if>
    </#if>

</@layout.registrationLayout>
