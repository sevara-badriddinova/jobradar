"""Country names and ISO codes for the US-only location filter.

Structured location fields (Workday's "DEU - Saxony - Dresden", a Workable
country, an ISO code) are the strongest evidence of where a job is. City-only
strings stay "unknown" unless they are unambiguous; the filter never rejects on
a city name that is also a US place without a state code.
"""

# ISO 3166-1 alpha-3. US and its territories (H-1B/OPT apply there) are "us".
US_ISO3 = frozenset({"USA", "PRI", "GUM", "VIR", "ASM", "MNP", "UMI"})
ISO3 = frozenset("""
ABW AFG AGO AIA ALA ALB AND ARE ARG ARM ASM ATA ATF ATG AUS AUT AZE BDI BEL BEN BES BFA BGD BGR
BHR BHS BIH BLM BLR BLZ BMU BOL BRA BRB BRN BTN BVT BWA CAF CAN CCK CHE CHL CHN CIV CMR COD COG
COK COL COM CPV CRI CUB CUW CXR CYM CYP CZE DEU DJI DMA DNK DOM DZA ECU EGY ERI ESH ESP EST ETH
FIN FJI FLK FRA FRO FSM GAB GBR GEO GGY GHA GIB GIN GLP GMB GNB GNQ GRC GRD GRL GTM GUF GUM GUY
HKG HMD HND HRV HTI HUN IDN IMN IND IOT IRL IRN IRQ ISL ISR ITA JAM JEY JOR JPN KAZ KEN KGZ KHM
KIR KNA KOR KWT LAO LBN LBR LBY LCA LIE LKA LSO LTU LUX LVA MAC MAF MAR MCO MDA MDG MDV MEX MHL
MKD MLI MLT MMR MNE MNG MNP MOZ MRT MSR MTQ MUS MWI MYS MYT NAM NCL NER NFK NGA NIC NIU NLD NOR
NPL NRU NZL OMN PAK PAN PCN PER PHL PLW PNG POL PRI PRK PRT PRY PSE PYF QAT REU ROU RUS RWA SAU
SDN SEN SGP SGS SHN SJM SLB SLE SLV SMR SOM SPM SRB SSD STP SUR SVK SVN SWE SWZ SXM SYC SYR TCA
TCD TGO THA TJK TKL TKM TLS TON TTO TUN TUR TUV TWN TZA UGA UKR UMI URY USA UZB VAT VCT VEN VGB
VIR VNM VUT WLF WSM XKX YEM ZAF ZMB ZWE
""".split())

# Country names (normalized: lowercase ASCII words) other than the United States.
# Deliberately excludes names that are also common US places ("georgia",
# "jordan", "lebanon"): a US state code or a US word always wins anyway.
COUNTRIES = frozenset("""
afghanistan|albania|algeria|andorra|angola|argentina|armenia|australia|austria|azerbaijan|bahamas|
bahrain|bangladesh|barbados|belarus|belgium|belize|benin|bermuda|bhutan|bolivia|bosnia and herzegovina|
bosnia|botswana|brazil|brasil|brunei|bulgaria|burkina faso|burundi|cambodia|cameroon|canada|
cape verde|cayman islands|chile|china|colombia|costa rica|cote d ivoire|ivory coast|croatia|
curacao|cyprus|czech republic|czechia|denmark|dominican republic|ecuador|egypt|el salvador|estonia|
eswatini|ethiopia|fiji|finland|france|gabon|gambia|germany|deutschland|ghana|gibraltar|greece|
guatemala|guernsey|guyana|haiti|honduras|hong kong|hong kong sar|hungary|iceland|india|indonesia|
iran|iraq|ireland|isle of man|israel|italy|italia|japan|kazakhstan|kenya|kosovo|
kuwait|kyrgyzstan|laos|latvia|lesotho|liberia|libya|liechtenstein|lithuania|luxembourg|macau|macao|
madagascar|malawi|malaysia|maldives|mali|malta|mauritius|mexico|moldova|monaco|mongolia|montenegro|
morocco|mozambique|myanmar|namibia|nepal|netherlands|the netherlands|new zealand|nicaragua|
niger|nigeria|north macedonia|macedonia|norway|oman|pakistan|palestine|paraguay|peru|philippines|
poland|polska|portugal|qatar|romania|russia|russian federation|rwanda|saudi arabia|senegal|serbia|
seychelles|sierra leone|singapore|slovakia|slovak republic|slovenia|south africa|south korea|korea|
republic of korea|spain|espana|sri lanka|sudan|suriname|sweden|sverige|switzerland|schweiz|suisse|
syria|taiwan|tajikistan|tanzania|thailand|togo|trinidad and tobago|tunisia|turkey|turkiye|
turkmenistan|uganda|ukraine|united arab emirates|uae|united kingdom|uk|great britain|
england|scotland|wales|northern ireland|uruguay|uzbekistan|venezuela|vietnam|viet nam|yemen|zambia|
zimbabwe
""".replace("\n", "").split("|"))

# Unambiguous non-US cities and regions (none is also a notable US place).
CITIES = frozenset("""
emea|apac|latam|europe|asia|africa|middle east|
london|dublin|toronto|vancouver|montreal|ottawa|calgary|edmonton|mississauga|winnipeg|
berlin|munich|munchen|hamburg|frankfurt|cologne|koln|stuttgart|dusseldorf|dresden|leipzig|nuremberg|
nurnberg|karlsruhe|bonn|paris|lyon|toulouse|marseille|grenoble|sophia antipolis|amsterdam|rotterdam|
eindhoven|utrecht|the hague|antwerp|brussels|ghent|madrid|barcelona|seville|bilbao|malaga|lisbon|
porto|braga|milano|roma|turin|torino|bologna|stockholm|gothenburg|malmo|
copenhagen|aarhus|oslo|helsinki|espoo|tampere|reykjavik|zurich|lausanne|basel|zug|vienna austria|
wien|graz|linz|prague|praha|brno|bratislava|budapest|bucharest|bucuresti|cluj|cluj napoca|iasi|
timisoara|sofia bulgaria|plovdiv|warsaw|warszawa|krakow|wroclaw|gdansk|poznan|lodz|katowice|vilnius|
kaunas|riga|tallinn|tartu|kyiv|kiev|lviv|kharkiv|belgrade|novi sad|zagreb|ljubljana|sarajevo|skopje|
tirana|athens greece|thessaloniki|istanbul|ankara|izmir|nicosia|limassol|valletta|edinburgh|glasgow|
belfast|cardiff|leeds|bristol uk|cork|galway|limerick|tel aviv|herzliya|haifa|jerusalem|petah tikva|
dubai|abu dhabi|riyadh|jeddah|doha|kuwait city|manama|muscat|casablanca|tunis|lagos|abuja|
nairobi|accra|kampala|kigali|johannesburg|cape town|pretoria|durban|
bangalore|bengaluru|hyderabad|pune|mumbai|delhi|new delhi|noida|gurgaon|gurugram|chennai|kolkata|
ahmedabad|kochi|coimbatore|thane|jaipur|trivandrum|thiruvananthapuram|mysore|mysuru|indore|nagpur|
chandigarh|karachi|lahore|islamabad|dhaka|colombo|kathmandu|
singapore|kuala lumpur|penang|cyberjaya|johor bahru|bangkok|ho chi minh|ho chi minh city|hanoi|
ha noi|da nang|jakarta|bandung|manila|makati|taguig|quezon city|pasig|cebu|
beijing|shanghai|shenzhen|guangzhou|hangzhou|chengdu|nanjing|suzhou|wuhan|xi an|tianjin|dalian|
qingdao|xiamen|hong kong|taipei|hsinchu|taichung|kaohsiung|tokyo|osaka|kyoto|yokohama|nagoya|
fukuoka|sapporo|seoul|busan|pangyo|
sydney|melbourne|brisbane|perth|adelaide|canberra|auckland|christchurch|
mexico city|ciudad de mexico|guadalajara|monterrey|tijuana|queretaro|bogota|medellin|cali colombia|
santiago chile|buenos aires|cordoba argentina|montevideo|sao paulo|rio de janeiro|curitiba|
belo horizonte|porto alegre|florianopolis|recife|campinas|san jose costa rica|guatemala city|
panama city panama|quito|caracas
""".replace("\n", "").split("|"))
