"""Keyword-based SOC 2020 classifier for job titles.

Maps job titles to SOC 2020 2-digit sub-major group codes using ordered
keyword rules. Falls back to 1-digit major group when a more specific match
isn't found.

This is a pragmatic heuristic classifier — not an ML model. It handles the
most common UK job titles well enough to connect ~70-80% of Reed listings to
ASHE percentile distributions. Titles that don't match any rule get None.

SOC 2020 hierarchy (what ASHE Table 2 provides):
    1-digit: Major group      (e.g. "2" = Professional occupations)
    2-digit: Sub-major group  (e.g. "21" = Science/engineering professionals)

Rules are evaluated in order; first match wins. More specific patterns come
before broader ones.
"""
from __future__ import annotations

import re
from typing import Optional


# Each rule: (compiled_regex, soc_code, description)
# Regex is matched against the lowercased job title.
_RULES: list[tuple[re.Pattern, str, str]] = []


def _r(pattern: str, soc: str, desc: str) -> None:
    _RULES.append((re.compile(pattern, re.IGNORECASE), soc, desc))


# ── SOC 11: Corporate managers and directors ──────────────────────
_r(r"\b(ceo|chief executive|managing director|md\b)", "11", "Corporate managers")
_r(r"\b(cto|cfo|coo|cio|ciso|chief\s+(technology|financial|operating|information))", "11", "Corporate managers")
_r(r"\b(director|head of|vp\b|vice president)", "11", "Corporate managers")

# ── SOC 12: Other managers and proprietors ────────────────────────
_r(r"\b(store manager|shop manager|restaurant manager|hotel manager|pub manager)", "12", "Other managers")
_r(r"\b(farm manager|estate manager|property manager)", "12", "Other managers")
_r(r"\b(branch partner)", "12", "Other managers")
_r(r"\b(franchise|proprietor)", "12", "Other managers")

# ── SOC 21: Science, research, engineering and technology professionals
_r(r"\b(software engineer|software developer|devops|sre\b|platform engineer)", "21", "Tech professionals")
_r(r"\b(data engineer|data scientist|machine learning|ml engineer|ai engineer)", "21", "Tech professionals")
_r(r"\b(full[- ]?stack|front[- ]?end|back[- ]?end|web developer|cloud engineer)", "21", "Tech professionals")
_r(r"\b(solutions? architect|technical architect|systems? architect)", "21", "Tech professionals")
_r(r"\b(qa engineer|test engineer|automation engineer|sdet)", "21", "Tech professionals")
_r(r"\b(cyber security|information security|security engineer|security analyst|penetration tester|soc analyst)", "21", "Tech professionals")
_r(r"\b(network engineer|infrastructure engineer|systems? engineer)", "21", "Tech professionals")
_r(r"\b(mechanical engineer|electrical engineer|civil engineer|structural engineer)", "21", "Tech professionals")
_r(r"\b(chemical engineer|process engineer|manufacturing engineer)", "21", "Tech professionals")
_r(r"\b(biomedical|physicist|chemist|biologist|scientist|researcher)", "21", "Tech professionals")
_r(r"\b(engineering\s+geologist|geologist|geotechnical)", "21", "Tech professionals")
_r(r"\b(energy\s+modeller|modeller)\b", "21", "Tech professionals")
_r(r"\b(urban\s+design)", "21", "Tech professionals")
_r(r"\b(ecologist|senior ecologist|conservation)", "21", "Tech professionals")
_r(r"\b(architectural\s+technologist)", "21", "Tech professionals")
_r(r"\b(architect)\b(?!.*\b(naval|landscape|interior))", "21", "Tech professionals")
_r(r"\b(quantity surveyor|building surveyor|surveyor)", "21", "Tech professionals")

# ── SOC 22: Health professionals ──────────────────────────────────
_r(r"\b(doctor|physician|consultant\s+(anaesth|cardiolog|oncolog|paediatr|surgeon|psychiatr))", "22", "Health professionals")
_r(r"\b(dentist|pharmacist|optometrist|dispensing\s+optician|veterinar|vet\b|radiographer)", "22", "Health professionals")
_r(r"\b(audiolog|audiology)", "22", "Health professionals")
_r(r"\b(physiotherapist|occupational therapist|speech.*(therap|language))", "22", "Health professionals")
_r(r"\b(nurse|nursing|midwi)", "22", "Health professionals")
_r(r"\b(paramedic|psychologist|psychology\s+graduate|clinical)", "22", "Health professionals")
_r(r"\b(phlebotomist)", "22", "Health professionals")

# ── SOC 23: Teaching and other educational professionals ──────────
_r(r"\b(teacher|headteacher|head teacher|lecturer|professor)", "23", "Teaching professionals")
_r(r"\b(teaching assistant|ta\b.*school|ect\b|nqt\b)", "23", "Teaching professionals")
_r(r"\bsen\s+ta\b", "23", "Teaching professionals")
_r(r"\bsenco\b", "23", "Teaching professionals")
_r(r"\b(trainer)\b", "23", "Teaching professionals")
_r(r"\bhlta\b", "23", "Teaching professionals")
_r(r"\b(cover supervisor|behaviour mentor|learning mentor)", "23", "Teaching professionals")
_r(r"\b(tutor|instructor)\b(?!.*\b(gym|fitness|driving))", "23", "Teaching professionals")
_r(r"\b(exam\s+invigilator|invigilator)", "23", "Teaching professionals")

# ── SOC 24: Business, media and public service professionals ──────
_r(r"\b(solicitor|barrister|lawyer|legal\s+(counsel|advisor|executive))", "24", "Business/legal professionals")
_r(r"\b(accountant|auditor|tax\s+(manager|advisor|specialist))", "24", "Business/legal professionals")
_r(r"\b(accounts?\s+senior|audit\s+senior|accounts?\s+payable|accounts?\s+receivable)", "24", "Business/legal professionals")
_r(r"\b(personal\s+tax\s+senior|corporate\s+tax\s+senior|tax\s+senior)", "24", "Business/legal professionals")
_r(r"\b(audit\s+semi\s+senior)", "24", "Business/legal professionals")
_r(r"\b(paralegal|conveyancing\s+paralegal|private\s+client\s+paralegal)", "24", "Business/legal professionals")
_r(r"\b(paraplanner|senior paraplanner)", "24", "Business/legal professionals")
_r(r"\b(financial\s+advis[eo]r)", "24", "Business/legal professionals")
_r(r"\b(finance\s+business\s+partner)", "24", "Business/legal professionals")
_r(r"\b(conveyancer|residential\s+conveyancer)", "24", "Business/legal professionals")
_r(r"\b(actuary|underwriter)", "24", "Business/legal professionals")
_r(r"\b(journalist|editor|copywriter|content\s+(writer|strategist))", "24", "Business/legal professionals")
_r(r"\b(social worker|probation officer)", "24", "Business/legal professionals")
_r(r"\b(planner)\b(?!.*\b(production|demand|supply))", "24", "Business/legal professionals")
_r(r"\b(grad\w*\s+scheme|graduate\s+scheme)", "24", "Business/legal professionals")

# ── SOC 31: Science, engineering and technology associate professionals
_r(r"\b(it\s+(support|technician|administrator|helpdesk|analyst))", "31", "Tech associates")
_r(r"\b(it\s+apprentice|apprentice\s+(it|software|developer|engineer))", "31", "Tech associates")
_r(r"\b(digital\s+(support\s+)?apprentice)", "31", "Tech associates")
_r(r"\b(lab technician|laboratory technician|dental technician)", "31", "Tech associates")
_r(r"\b(draughtsperson|cad technician|building control)", "31", "Tech associates")

# ── SOC 32: Health and social care associate professionals ────────
_r(r"\b(healthcare assistant|hca\b|care coordinator)", "32", "Health associates")
_r(r"\b(dental (nurse|hygienist)|pharmacy technician)", "32", "Health associates")
_r(r"\b(ambulance|paramedic technician|counsellor)", "32", "Health associates")

# ── SOC 33: Protective service occupations ────────────────────────
_r(r"\b(police|fire\s*(fighter|officer)|prison officer|security officer)", "33", "Protective service")
_r(r"\b(customs|border force|immigration officer)", "33", "Protective service")

# ── SOC 34: Culture, media and sports occupations ─────────────────
_r(r"\b(graphic designer|ux|ui designer|product designer)", "34", "Culture/media")
_r(r"\b(photographer|videographer|animator|illustrator)", "34", "Culture/media")
_r(r"\b(fitness instructor|personal trainer|sports coach|gym)", "34", "Culture/media")
_r(r"\b(chef|sous chef|commis chef|pastry chef|head chef)", "34", "Culture/media")

# ── SOC 35: Business and public service associate professionals ───
_r(r"\b(business analyst|data analyst|financial analyst|management consultant)", "35", "Business associates")
_r(r"\b(project manager|programme manager|scrum master|product (manager|owner))", "35", "Business associates")
_r(r"\b(hr\s+(manager|advisor|officer|business partner|coordinator|generalist))", "35", "Business associates")
_r(r"\b(pension|pensions)", "35", "Business associates")
_r(r"\b(employment\s+specialist)", "35", "Business associates")
_r(r"\b(recruiter|recruitment\s+(consultant|manager|advisor))", "35", "Business associates")
_r(r"\b(marketing\s+(manager|executive|coordinator|analyst))", "35", "Business associates")
_r(r"\b(social\s+media\s+(executive|manager|coordinator))", "35", "Business associates")
_r(r"\b(senior data strategist|data strategist)", "35", "Business associates")
_r(r"\b(commercial account handler|account handler)", "35", "Business associates")
_r(r"\b(buyer|procurement|purchasing)", "35", "Business associates")
_r(r"\b(estate agent|letting)", "35", "Business associates")
_r(r"\b(insurance\s+(broker|advisor|underwriter))", "35", "Business associates")
_r(r"\b(mortgage\s+(advisor|adviser|broker))", "35", "Business associates")
_r(r"\b(equity release\s+broker)", "35", "Business associates")
_r(r"\b(estimator)\b", "35", "Business associates")
_r(r"\b(property\s+valuer)", "35", "Business associates")
_r(r"\b(hire\s+controller)", "35", "Business associates")
_r(r"\b(client\s+engagement)", "35", "Business associates")
_r(r"\b(contracts?\s+(officer|commissioning))", "35", "Business associates")
_r(r"\b(compliance|risk\s+(analyst|manager|officer))", "35", "Business associates")
_r(r"\b(financial\s+(advisor|planner|controller))", "35", "Business associates")

# ── SOC 41: Administrative occupations ────────────────────────────
_r(r"\b(administrator|admin\s+assistant|office\s+(manager|assistant|administrator))", "41", "Administrative")
_r(r"\b(receptionist|personal assistant|pa\b|executive assistant|ea\b)", "41", "Administrative")
_r(r"\b(data entry|filing|clerk)", "41", "Administrative")
_r(r"\b(administration)\b", "41", "Administrative")
_r(r"\b(claims\s+handler)", "41", "Administrative")
_r(r"\b(bookkeeper|payroll|credit control|accounts\s+(assistant|clerk))", "41", "Administrative")

# ── SOC 42: Secretarial and related occupations ───────────────────
_r(r"\b(secretary|legal secretary|medical secretary)", "42", "Secretarial")
_r(r"\b(typist|transcri)", "42", "Secretarial")

# ── SOC 51: Skilled agricultural and related trades ───────────────
_r(r"\b(farmer|groundskeeper|groundsman|landscaper|gardener|arborist|tree surgeon)", "51", "Agricultural trades")
_r(r"\b(gamekeeper|horticult|florist)", "51", "Agricultural trades")

# ── SOC 52: Skilled metal, electrical and electronic trades ───────
_r(r"\b(electrician|plumber|gas engineer|heating engineer|hvac)", "52", "Metal/electrical trades")
_r(r"\b(welder|fitter|machinist|toolmaker|sheet metal)", "52", "Metal/electrical trades")
_r(r"\b(pipefitter)", "52", "Metal/electrical trades")
_r(r"\b(auto\s*(mechanic|technician)|mot\s+tester|vehicle technician|car mechanic)", "52", "Metal/electrical trades")
_r(r"\b(mechanic)\b", "52", "Metal/electrical trades")
_r(r"\b(telecommunications|fibre|cable\s+(engineer|technician))", "52", "Metal/electrical trades")

# ── SOC 53: Skilled construction and building trades ──────────────
_r(r"\b(bricklayer|carpenter|joiner|plasterer|roofer|scaffolder)", "53", "Construction trades")
_r(r"\b(painter|decorator|tiler|glazier|window fitter)", "53", "Construction trades")
_r(r"\b(site manager|construction manager|foreman)", "53", "Construction trades")

# ── SOC 54: Textiles, printing and other skilled trades ───────────
_r(r"\b(tailor|seamstress|upholster|printer|sign\s*(writer|maker))", "54", "Textiles/printing trades")
_r(r"\b(butcher|baker|fishmonger)", "54", "Textiles/printing trades")
_r(r"\b(locksmith|jeweller|watchmaker)", "54", "Textiles/printing trades")

# ── SOC 61: Caring personal service occupations ──────────────────
_r(r"\b(care (worker|assistant)|carer|support worker|domiciliary)", "61", "Caring services")
_r(r"\b(nursery|childminder|nanny|childcare|playworker)", "61", "Caring services")
_r(r"\b(early years practitioner|early years educator|early years level|room leader)", "61", "Caring services")
_r(r"\b(health and social care assessor|social care assessor)", "61", "Caring services")
_r(r"\b(youth worker|aspiring youth worker)", "61", "Caring services")
_r(r"\b(home care|residential care|care home)", "61", "Caring services")

# ── SOC 62: Leisure, travel and related personal service ──────────
_r(r"\b(hairdresser|barber|beautician|beauty therapist|nail technician)", "62", "Leisure/travel services")
_r(r"\b(travel agent|tour guide|cabin crew|flight attendant)", "62", "Leisure/travel services")
_r(r"\b(waiter|waitress|bartender|barista|front of house|nandoca|back of house)", "62", "Leisure/travel services")
_r(r"\b(waiting staff|bar.*(staff|team))", "62", "Leisure/travel services")
_r(r"\b(hotel|housekeep|concierge|porter\b(?!.*\bnight))", "62", "Leisure/travel services")

# ── SOC 63: Community and civil enforcement ───────────────────────
_r(r"\b(community\s+(officer|warden|worker)|civil enforcement|traffic warden|parking)", "63", "Community/enforcement")
_r(r"\b(neighbourhood\s+officer|housing\s+officer|enforcement\s+agent)", "63", "Community/enforcement")
_r(r"\b(tenancy\s+specialist|tenancy)", "63", "Community/enforcement")
_r(r"\b(client\s+liaison\s+officer)", "63", "Community/enforcement")

# ── SOC 71: Sales occupations ─────────────────────────────────────
_r(r"\b(sales\s+(assistant|advisor|executive|representative|consultant|manager))", "71", "Sales")
_r(r"\b(retail\s+(assistant|advisor|manager|supervisor))", "71", "Sales")
_r(r"\b(account\s+(manager|executive|director))", "71", "Sales")
_r(r"\b(business development|bdm\b|telesales|telemarketer)", "71", "Sales")
_r(r"\b(sales development representative|sdr\b)", "71", "Sales")
_r(r"\b(fundraiser|fundraising)", "71", "Sales")
_r(r"\b(sales negotiator|sales valuer)", "71", "Sales")
_r(r"\b(sales support|internal sales)", "71", "Sales")
_r(r"\b(career starter)", "71", "Sales")
_r(r"\b(shop assistant|cashier|checkout)", "71", "Sales")

# ── SOC 72: Customer service occupations ──────────────────────────
_r(r"\b(customer\s+(service|support|advisor|representative|success))", "72", "Customer service")
_r(r"\b(call centre|contact centre|complaints|helpdesk|service desk)", "72", "Customer service")
_r(r"\b(call handler|emergency call handler)", "72", "Customer service")

# ── SOC 81: Process, plant and machine operatives ─────────────────
_r(r"\b(machine operator|production operator|process operator)", "81", "Machine operatives")
_r(r"\b(assembler|packer|picker|warehouse operative)", "81", "Machine operatives")
_r(r"\b(selector)\b", "81", "Machine operatives")
_r(r"\b(telehandler)", "81", "Machine operatives")
_r(r"\b(cnc|press operator|lathe)", "81", "Machine operatives")

# ── SOC 82: Transport and mobile machine drivers ─────────────────
_r(r"\b(driver|hgv|lgv|van driver|bus driver|lorry driver|chauffeur)", "82", "Transport/drivers")
_r(r"\b(forklift|crane operator|plant operator|excavator)", "82", "Transport/drivers")
_r(r"\b(delivery driver|courier)", "82", "Transport/drivers")

# ── SOC 91: Elementary trades and related ─────────────────────────
_r(r"\b(labourer|laborer|construction worker|demolition)", "91", "Elementary trades")
_r(r"\b(farm worker|agricultural worker)", "91", "Elementary trades")

# ── SOC 92: Elementary administration and service ─────────────────
_r(r"\b(cleaner|cleaning|janitor|caretaker)", "92", "Elementary admin/service")
_r(r"\b(kitchen (assistant|porter|team member)|pot wash)", "92", "Elementary admin/service")
_r(r"\b(valeter|valet\b)", "92", "Elementary admin/service")
_r(r"\b(field\s+collection\s+agent)", "92", "Elementary admin/service")
_r(r"\b(security guard|door supervisor|steward)", "92", "Elementary admin/service")
_r(r"\b(postal|mail|sorting office|postman)", "92", "Elementary admin/service")

# ── Generic leadership roles (confirmed SOC 1) ──────────────────
_r(r"\b(supervisor)\b", "1", "Supervisor (generic)")
_r(r"\b(team leader)\b", "1", "Team leader")

# ── Broader fallbacks using major group (1-digit) ─────────────────
_r(r"\bmanager\b", "1", "Manager (generic)")
_r(r"\b(engineer|developer|programmer|architect)\b", "2", "Professional (generic)")
_r(r"\b(analyst|consultant|coordinator|advisor)\b", "3", "Associate professional (generic)")
_r(r"\b(assistant|administrator|clerk)\b", "4", "Admin (generic)")
_r(r"\b(technician|operative|operator)\b", "8", "Operative (generic)")
_r(r"\b(trainee)\b", "3", "Trainee (default associate)")


def classify_title(title: str) -> Optional[str]:
    """Return the best-matching SOC 2020 code for a job title, or None."""
    if not title or not title.strip():
        return None
    for pattern, soc_code, _desc in _RULES:
        if pattern.search(title):
            return soc_code
    return None


def classify_title_verbose(title: str) -> tuple[Optional[str], Optional[str]]:
    """Return (soc_code, rule_description) for debugging."""
    if not title or not title.strip():
        return None, None
    for pattern, soc_code, desc in _RULES:
        if pattern.search(title):
            return soc_code, desc
    return None, None


if __name__ == "__main__":
    # Quick test
    test_titles = [
        "Software Engineer", "Senior Data Scientist", "Trainee Solicitor",
        "Teaching Assistant", "HGV Driver", "Registered Nurse",
        "Retail Sales Assistant", "Project Manager", "Head Chef",
        "Warehouse Operative", "Cyber Security Analyst", "HR Administrator",
        "Junior Accountant", "Care Worker", "Electrician",
        "Receptionist", "Business Development Manager", "Delivery Driver",
        # Newly added from unclassified review:
        "Accounts Senior", "Audit Senior", "Architectural Technologist",
        "HLTA", "Cover Supervisor", "Behaviour Mentor", "Paraplanner",
        "Senior Paraplanner", "Financial Adviser", "Finance Business Partner",
        "Residential Conveyancer", "BD Grad Scheme", "Commercial Graduate Scheme",
        "Early Years Practitioner", "Room Leader", "Sales Negotiator",
        "Sales Valuer", "Career Starter Stores", "Senior Data Strategist - CRM",
        "Commercial Account Handler", "Branch Partner", "Supervisor",
        "Team Leader", "Senior Ecologist", "Dispensing Optician",
    ]
    for t in test_titles:
        code, desc = classify_title_verbose(t)
        print(f"  {t:40s} → SOC {code or '???':3s}  ({desc or 'no match'})")
