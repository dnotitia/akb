<#-- Consent page. Replaces keycloak.v2/login/login-oauth-grant.ftl.
     MCP clients register themselves and ask for AKB's vault scopes, so this is
     the page that decides what an agent may do. It names the client and lists
     each permission by its consent text, as upstream does. Keycloak gives the
     template no scope names (26.7: consentScreenText, guiOrder and the
     parameter only), so a scope is worded by its consent text: a literal is
     shown as is, a ${key} is looked up in these messages. -->
<#import "template.ftl" as layout>
<#assign akbClientName = (client.name?has_content)?then(advancedMsg(client.name), client.clientId)>
<@layout.registrationLayout bodyClass="oauth"; section>
    <#if section = "header">
        ${msg("akbConsentTitle", akbClientName)}
    <#elseif section = "subtitle">
        ${msg("akbConsentLead", akbClientName)}
    <#elseif section = "form">
        <div id="kc-oauth" class="content-area akb-consent">
            <ul class="akb-consent__scopes">
                <#if oauth.clientScopesRequested??>
                    <#list oauth.clientScopesRequested as clientScope>
                        <li class="akb-consent__scope">
                            <span class="akb-consent__check" aria-hidden="true"><@layout.icon name="success"/></span>
                            <span><#if !clientScope.parameterizedScopeParameter??>${advancedMsg(clientScope.consentScreenText)}<#else>${advancedMsg(clientScope.consentScreenText, clientScope.parameterizedScopeParameter)}</#if></span>
                        </li>
                    </#list>
                </#if>
            </ul>
            <#if client.attributes.policyUri?? || client.attributes.tosUri??>
                <p class="akb-consent__terms">
                    ${msg("oauthGrantInformation", akbClientName)}
                    <#if client.attributes.tosUri??>
                        ${msg("oauthGrantReview")}
                        <a href="${client.attributes.tosUri}" target="_blank" rel="noopener noreferrer">${msg("oauthGrantTos")}</a>
                    </#if>
                    <#if client.attributes.policyUri??>
                        ${msg("oauthGrantReview")}
                        <a href="${client.attributes.policyUri}" target="_blank" rel="noopener noreferrer">${msg("oauthGrantPolicy")}</a>
                    </#if>
                </p>
            </#if>
            <p class="akb-consent__revoke">${msg("akbConsentRevokeHint")}</p>

            <form class="${properties.kcFormClass} akb-consent__form" action="${url.oauthAction}" method="POST">
                <input type="hidden" name="code" value="${oauth.code}">
                <div class="akb-form-actions akb-form-actions--split">
                    <button class="akb-button akb-button--secondary" name="cancel" id="kc-cancel" type="submit">${msg("akbConsentDeny")}</button>
                    <button class="akb-button akb-button--primary" name="accept" id="kc-login" type="submit">${msg("akbConsentAllow")}</button>
                </div>
            </form>
        </div>
    </#if>
</@layout.registrationLayout>
