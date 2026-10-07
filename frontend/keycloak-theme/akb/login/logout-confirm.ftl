<#-- Sign-out confirmation. Replaces base/login/logout-confirm.ftl: same form,
     AKB layout. -->
<#import "template.ftl" as layout>
<@layout.registrationLayout; section>
    <#if section = "header">
        ${msg("logoutConfirmTitle")}
    <#elseif section = "subtitle">
        ${msg("logoutConfirmHeader")}
    <#elseif section = "form">
        <div id="kc-logout-confirm" class="content-area akb-message">

            <form class="form-actions" action="${url.logoutConfirmAction}" onsubmit="confirmLogout.disabled = true; return true;" method="POST">
                <input type="hidden" name="session_code" value="${logoutConfirm.code}">
                <div class="akb-form-actions">
                    <input class="akb-button akb-button--primary akb-button--block"
                           name="confirmLogout" id="kc-logout" type="submit" value="${msg("doLogout")}"/>
                </div>
            </form>

            <div id="kc-info-message">
                <#if logoutConfirm.skipLink>
                <#else>
                    <#if (client.baseUrl)?has_content>
                        <p class="akb-form-actions"><a class="akb-button akb-button--secondary akb-button--block" href="${client.baseUrl}">${msg("backToApplication")}</a></p>
                    </#if>
                </#if>
            </div>
        </div>
    </#if>
</@layout.registrationLayout>
