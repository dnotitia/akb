<#-- Sign-in page. Replaces keycloak.v2/login/login.ftl.
     Laid out like Astryx's login card: the realm's own account form and its
     primary button, then "or continue with" and one quiet button per identity
     provider.
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
    <#elseif section = "subtitle">
        <#if akbNativeOnly>${msg("akbAdminLoginLead")}<#else>${msg("akbLoginLead")}</#if>
    <#elseif section = "form">
        <div id="kc-form">
          <div id="kc-form-wrapper">
            <#if realm.password>
                <form id="kc-form-login" class="${properties.kcFormClass!}" onsubmit="login.disabled = true; return true;" action="${url.loginAction}" method="post" novalidate="novalidate">
                    <#if !usernameHidden??>
                        <#assign label>
                            <#if !realm.loginWithEmailAllowed>${msg("username")}<#elseif !realm.registrationEmailAsUsername>${msg("usernameOrEmail")}<#else>${msg("email")}</#if>
                        </#assign>
                        <@field.input name="username" label=label error=messagesPerField.getFirstError('username','password')
                            autofocus=true autocomplete="${(enableWebAuthnConditionalUI?has_content)?then('username webauthn', 'username')}" value=login.username!'' />
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
                      <button class="akb-button akb-button--primary akb-button--block" name="login" id="kc-login" type="submit">${msg("akbLoginAction")}</button>
                    </div>
                </form>
            </#if>
          </div>
        </div>
        <@passkeys.conditionalUIData />

        <#if akbProviders>
          <div id="kc-social-providers" class="akb-providers">
            <#if realm.password>
              <p class="akb-divider"><span>${msg("akbOrContinueWith")}</span></p>
            </#if>
            <ul class="akb-providers__list">
              <#list social.providers as p>
                <li>
                  <a data-once-link data-disabled-class="akb-button--busy" id="social-${p.alias}"
                     class="akb-button akb-button--secondary akb-button--block akb-provider"
                     href="${p.loginUrl}">${msg("akbIdentityProviderAction", p.displayName!p.alias)}</a>
                </li>
              </#list>
            </ul>
          </div>
        </#if>
    <#elseif section = "socialProviders" >
        <#-- Drawn after the form, in "form". -->
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
