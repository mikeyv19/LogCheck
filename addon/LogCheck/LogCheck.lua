-- LogCheck: copy every LFG applicant's Name-Realm at once for the LogCheck desktop app.
-- Click "LogCheck" on your group's applicant list (or type /logcheck), press Ctrl+C,
-- then Ctrl+V in LogCheck. The "LFG:" prefix tells LogCheck this is the whole applicant list,
-- so it only looks up new applicants and removes the ones who left.

local PREFIX = "LFG:"
local ACTIVE = { applied = true, invited = true }  -- still in your queue

local function ApplicantNames()
    local realm = GetNormalizedRealmName()
    local names, seen = {}, {}
    for _, id in ipairs(C_LFGList.GetApplicants() or {}) do
        local info = C_LFGList.GetApplicantInfo(id)
        if info and ACTIVE[info.pendingApplicationStatus or info.applicationStatus] then
            for i = 1, info.numMembers or 1 do
                local name = C_LFGList.GetApplicantMemberInfo(id, i)
                if name then
                    if not name:find("-", 1, true) then
                        name = name .. "-" .. realm  -- same-realm players come without a realm
                    end
                    if not seen[name] then
                        seen[name] = true
                        names[#names + 1] = name
                    end
                end
            end
        end
    end
    return names
end

local box
local function ShowCopyBox(text, count)
    if not box then
        local f = CreateFrame("Frame", "LogCheckCopyFrame", UIParent, "BackdropTemplate")
        f:SetSize(440, 76)
        f:SetPoint("CENTER")
        f:SetFrameStrata("DIALOG")
        f:SetBackdrop({
            bgFile = "Interface\\DialogFrame\\UI-DialogBox-Background",
            edgeFile = "Interface\\DialogFrame\\UI-DialogBox-Border",
            tile = true, tileSize = 32, edgeSize = 32,
            insets = { left = 11, right = 12, top = 12, bottom = 11 },
        })
        f.title = f:CreateFontString(nil, "OVERLAY", "GameFontNormal")
        f.title:SetPoint("TOP", 0, -18)

        local eb = CreateFrame("EditBox", nil, f, "InputBoxTemplate")
        eb:SetSize(390, 20)
        eb:SetPoint("BOTTOM", 0, 18)
        eb:SetAutoFocus(false)
        eb:SetScript("OnEscapePressed", function() f:Hide() end)
        eb:SetScript("OnKeyDown", function(_, key)
            if key == "C" and IsControlKeyDown() then
                C_Timer.After(0.1, function() f:Hide() end)  -- copied, done
            end
        end)
        eb:SetScript("OnTextChanged", function(self, userInput)
            if userInput then  -- a stray keypress shouldn't mangle the list
                self:SetText(f.text)
                self:HighlightText()
            end
        end)
        f.eb = eb
        tinsert(UISpecialFrames, "LogCheckCopyFrame")  -- Escape closes it
        box = f
    end
    box.text = text
    box.title:SetText(("%d applicant(s) - press Ctrl+C, then Ctrl+V in LogCheck"):format(count))
    box.eb:SetText(text)
    box:Show()
    box.eb:SetFocus()
    box.eb:HighlightText()
end

local function CopyApplicants()
    if not C_LFGList.HasActiveEntryInfo() then
        print("|cffe5cc80LogCheck:|r you don't have a group listed in the Group Finder.")
        return
    end
    local names = ApplicantNames()
    ShowCopyBox(PREFIX .. table.concat(names, ", "), #names)
end

local function AddButton()
    local viewer = LFGListFrame and LFGListFrame.ApplicationViewer
    if not viewer or viewer.LogCheckButton then
        return
    end
    local b = CreateFrame("Button", nil, viewer, "UIPanelButtonTemplate")
    b:SetSize(80, 22)
    b:SetText("LogCheck")
    -- Top-right of the listing info panel: empty space, clear of the column headers
    b:SetPoint("TOPRIGHT", viewer.InfoBackground or viewer, "TOPRIGHT", -6, -6)
    b:SetScript("OnClick", CopyApplicants)
    b:SetScript("OnEnter", function(self)
        GameTooltip:SetOwner(self, "ANCHOR_TOP")
        GameTooltip:SetText("Copy all applicants for LogCheck")
        GameTooltip:Show()
    end)
    b:SetScript("OnLeave", GameTooltip_Hide)
    viewer.LogCheckButton = b
end

local events = CreateFrame("Frame")
events:RegisterEvent("PLAYER_LOGIN")
events:RegisterEvent("ADDON_LOADED")  -- in case the Group Finder UI loads later
events:SetScript("OnEvent", AddButton)

SLASH_LOGCHECK1 = "/logcheck"
SLASH_LOGCHECK2 = "/lcc"
SlashCmdList.LOGCHECK = CopyApplicants
