<#-- "Page has expired". Replaces base/login/login-page-expired.ftl.
     Keycloak shows this when a single-use step URL is requested twice: the
     2026-10-06 incident was a browser asking for the broker hop again one
     second after it had already been sent on to the identity provider. The
     sign-in itself was fine, so with scripts the page resumes it once per tab
     (js/akb-login.js) and only explains itself if it is shown again in that
     tab. Without scripts it explains straight away. data-akb-expired-target
     names the link that resumes; harness/README.md records why that one. -->
<#import "template.ftl" as layout>
<@layout.registrationLayout; section>
    <#if section = "header">
        <span class="akb-expired__title-resuming">${msg("akbExpiredResumingTitle")}</span>
        <span class="akb-expired__title-explain">${msg("akbExpiredTitle")}</span>
    <#elseif section = "subtitle">
        <span class="akb-expired__resuming" role="status">${msg("akbExpiredResuming")}</span>
        <span id="instruction1" class="akb-expired__explain instruction">${msg("akbExpiredExplain")}</span>
    <#elseif section = "form">
        <div id="akb-expired" class="akb-expired" data-akb-expired data-akb-expired-target="loginRestartLink">
            <div class="akb-expired__explain akb-form-actions akb-form-actions--stack">
                <a id="loginRestartLink" class="akb-button akb-button--primary akb-button--block" href="${url.loginRestartFlowUrl}">${msg("akbExpiredRestart")}</a>
                <a id="loginContinueLink" class="akb-button akb-button--secondary akb-button--block" href="${url.loginAction}">${msg("akbExpiredContinue")}</a>
            </div>
        </div>
    </#if>
</@layout.registrationLayout>
