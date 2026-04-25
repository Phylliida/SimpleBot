-- Data processing based on openmaptiles.org schema
-- https://openmaptiles.org/schema/
-- Copyright (c) 2016, KlokanTech.com & OpenMapTiles contributors.
-- Used under CC-BY 4.0

--------
-- Alter these lines to control which languages are written for place/streetnames
--
-- Preferred language can be (for example) "en" for English, "de" for German, or nil to use OSM's name tag:
preferred_language = nil
-- This is written into the following vector tile attribute (usually "name:latin"):
preferred_language_attribute = "name:latin"
-- If OSM's name tag differs, then write it into this attribute (usually "name_int"):
default_language_attribute = "name_int"
-- Also write these languages if they differ - for example, { "de", "fr" }
additional_languages = { }
--------

-- Enter/exit Tilemaker
function init_function()
end
function exit_function()
end

-- Implement Sets in tables
function Set(list)
	local set = {}
	for _, l in ipairs(list) do set[l] = true end
	return set
end

-- Meters per pixel if tile is 256x256
ZRES5  = 4891.97
ZRES6  = 2445.98
ZRES7  = 1222.99
ZRES8  = 611.5
ZRES9  = 305.7
ZRES10 = 152.9
ZRES11 = 76.4
ZRES12 = 38.2
ZRES13 = 19.1

-- The height of one floor, in meters
BUILDING_FLOOR_HEIGHT = 3.66

-- Process node/way tags
aerodromeValues = Set { "international", "public", "regional", "military", "private" }
pavedValues = Set { "paved", "asphalt", "cobblestone", "concrete", "concrete:lanes", "concrete:plates", "metal", "paving_stones", "sett", "unhewn_cobblestone", "wood" }
unpavedValues = Set { "unpaved", "compacted", "dirt", "earth", "fine_gravel", "grass", "grass_paver", "gravel", "gravel_turf", "ground", "ice", "mud", "pebblestone", "salt", "sand", "snow", "woodchips" }

-- Process node tags

node_keys = { "aerialway","aeroway","amenity","barrier","highway","historic","leisure","natural","office","place","railway","shop","sport","tourism","waterway" }

-- Get admin level which the place node is capital of.
-- Returns nil in case of invalid capital and for places which are not capitals.
function capitalLevel(capital)
	local capital_al = tonumber(capital) or 0
	if capital == "yes" then
		capital_al = 2
	end
	if capital_al == 0 then
		return nil
	end
        return capital_al
end

-- Calculate rank for place nodes
-- place: value of place=*
-- popuplation: population as number
-- capital_al: result of capitalLevel()
function calcRank(place, population, capital_al)
	local rank = 0
	if capital_al and capital_al >= 2 and capital_al <= 4 then
		rank = capital_al
		if population > 3 * 10^6 then
			rank = rank - 2
		elseif population > 1 * 10^6 then
			rank = rank - 1
		elseif population < 100000 then
			rank = rank + 2
		elseif population < 50000 then
			rank = rank + 3
		end
		-- Safety measure to avoid place=village/farm/... appear early (as important capital) because a mapper added capital=yes/2/3/4
		if place ~= "city" then
			rank = rank + 3
			-- Decrease rank further if it is not even a town.
			if place ~= "town" then
				rank = rank + 2
			end
		end
		return rank
	end
	if place ~= "city" and place ~= "town" then
		return nil
        end
	if population > 3 * 10^6 then
		return 1
	elseif population > 1 * 10^6 then
		return 2
	elseif population > 500000 then
		return 3
	elseif population > 200000 then
		return 4
	elseif population > 100000 then
		return 5
	elseif population > 75000 then
		return 6
	elseif population > 50000 then
		return 7
	elseif population > 25000 then
		return 8
	elseif population > 10000 then
		return 9
	end
	return 10
end


function node_function()
	-- Write 'aerodrome_label'
	local aeroway = Find("aeroway")
	if aeroway == "aerodrome" then
		Layer("aerodrome_label", false)
		SetNameAttributes()
		-- SLIM: stripped iata / icao / ele / ele_ft / class — client renders
		--       only the aerodrome name.
	end
	-- Housenumbers live in their own binary bundle served by /housenumbers,
	-- not in the vector tiles — they were ~15-25% of z14 tile bytes for
	-- features that only render at z17+.

	-- Write 'place'
	-- SLIM: client's `place` style filter keeps only
	--       {continent, country, state, city, town}; all other place classes
	--       (village, hamlet, suburb, neighbourhood, locality, farm, ...)
	--       are emitted but never rendered, so skip them entirely.
	local place = Find("place")
	if place ~= "" then
		if place == "continent" or place == "country" or place == "state"
		   or place == "city" or place == "town" then
			local mz = 13
			local pop = tonumber(Find("population")) or 0
			if     place == "continent"     then mz=0
			elseif place == "country"       then
				if     pop>50000000 then mz=1
				elseif pop>20000000 then mz=2
				else                     mz=3 end
			elseif place == "state"         then mz=4
			elseif place == "city"          then mz=5
			elseif place == "town" and pop>8000 then mz=7
			elseif place == "town"          then mz=8
			end
			Layer("place", false)
			Attribute("class", place)
			MinZoom(mz)
			SetNameAttributes()
		end
		return
	end

	-- Write 'poi'
	local rank, class, subclass = GetPOIRank()
	if rank then WritePOI(class,subclass,rank) end

	-- SLIM: `mountain_peak` layer dropped (not referenced by our map style).
	--       `water_name` kept for bays / lake names.
	local natural = Find("natural")
	if natural == "peak" or natural == "volcano" then
		-- dropped
		return
	end
	if natural == "bay" then
		Layer("water_name", false)
		SetNameAttributes()
		return
	end
end

-- Process way tags

majorRoadValues = Set { "motorway", "trunk", "primary" }
mainRoadValues  = Set { "secondary", "motorway_link", "trunk_link", "primary_link", "secondary_link" }
midRoadValues   = Set { "tertiary", "tertiary_link" }
minorRoadValues = Set { "unclassified", "residential", "road", "living_street" }
trackValues     = Set { "track" }
pathValues      = Set { "footway", "cycleway", "bridleway", "path", "steps", "pedestrian" }
linkValues      = Set { "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link" }
constructionValues = Set { "primary", "secondary", "tertiary", "motorway", "service", "trunk", "track" }
pavedValues     = Set { "paved", "asphalt", "cobblestone", "concrete", "concrete:lanes", "concrete:plates", "metal", "paving_stones", "sett", "unhewn_cobblestone", "wood" }
unpavedValues   = Set { "unpaved", "compacted", "dirt", "earth", "fine_gravel", "grass", "grass_paver", "gravel", "gravel_turf", "ground", "ice", "mud", "pebblestone", "salt", "sand", "snow", "woodchips" }

aerowayBuildings= Set { "terminal", "gate", "tower" }
landuseKeys     = Set { "school", "university", "kindergarten", "college", "library", "hospital",
                        "railway", "cemetery", "military", "residential", "commercial", "industrial",
                        "retail", "stadium", "pitch", "playground", "theme_park", "bus_station", "zoo" }
landcoverKeys   = { wood="wood", forest="wood",
                    wetland="wetland",
                    beach="sand", sand="sand",
                    farmland="farmland", farm="farmland", orchard="farmland", vineyard="farmland", plant_nursery="farmland",
                    glacier="ice", ice_shelf="ice",
                    grassland="grass", grass="grass", meadow="grass", allotments="grass", park="grass", village_green="grass", recreation_ground="grass", garden="grass", golf_course="grass" }

-- POI key/value pairs: based on https://github.com/openmaptiles/openmaptiles/blob/master/layers/poi/mapping.yaml
poiTags         = { aerialway = Set { "station" },
					amenity = Set { "arts_centre", "bank", "bar", "bbq", "bicycle_parking", "bicycle_rental", "biergarten", "bus_station", "cafe", "cinema", "clinic", "college", "community_centre", "courthouse", "dentist", "doctors", "embassy", "fast_food", "ferry_terminal", "fire_station", "food_court", "fuel", "grave_yard", "hospital", "ice_cream", "kindergarten", "library", "marketplace", "motorcycle_parking", "nightclub", "nursing_home", "parking", "pharmacy", "place_of_worship", "police", "post_box", "post_office", "prison", "pub", "public_building", "recycling", "restaurant", "school", "shelter", "swimming_pool", "taxi", "telephone", "theatre", "toilets", "townhall", "university", "veterinary", "waste_basket" },
					barrier = Set { "bollard", "border_control", "cycle_barrier", "gate", "lift_gate", "sally_port", "stile", "toll_booth" },
					building = Set { "dormitory" },
					highway = Set { "bus_stop" },
					historic = Set { "monument", "castle", "ruins" },
					landuse = Set { "basin", "brownfield", "cemetery", "reservoir", "winter_sports" },
					leisure = Set { "dog_park", "escape_game", "garden", "golf_course", "ice_rink", "hackerspace", "marina", "miniature_golf", "park", "pitch", "playground", "sports_centre", "stadium", "swimming_area", "swimming_pool", "water_park" },
					railway = Set { "halt", "station", "subway_entrance", "train_station_entrance", "tram_stop" },
					shop = Set { "accessories", "alcohol", "antiques", "art", "bag", "bakery", "beauty", "bed", "beverages", "bicycle", "books", "boutique", "butcher", "camera", "car", "car_repair", "carpet", "charity", "chemist", "chocolate", "clothes", "coffee", "computer", "confectionery", "convenience", "copyshop", "cosmetics", "deli", "delicatessen", "department_store", "doityourself", "dry_cleaning", "electronics", "erotic", "fabric", "florist", "frozen_food", "furniture", "garden_centre", "general", "gift", "greengrocer", "hairdresser", "hardware", "hearing_aids", "hifi", "ice_cream", "interior_decoration", "jewelry", "kiosk", "lamps", "laundry", "mall", "massage", "mobile_phone", "motorcycle", "music", "musical_instrument", "newsagent", "optician", "outdoor", "perfume", "perfumery", "pet", "photo", "second_hand", "shoes", "sports", "stationery", "supermarket", "tailor", "tattoo", "ticket", "tobacco", "toys", "travel_agency", "video", "video_games", "watches", "weapons", "wholesale", "wine" },
					sport = Set { "american_football", "archery", "athletics", "australian_football", "badminton", "baseball", "basketball", "beachvolleyball", "billiards", "bmx", "boules", "bowls", "boxing", "canadian_football", "canoe", "chess", "climbing", "climbing_adventure", "cricket", "cricket_nets", "croquet", "curling", "cycling", "disc_golf", "diving", "dog_racing", "equestrian", "fatsal", "field_hockey", "free_flying", "gaelic_games", "golf", "gymnastics", "handball", "hockey", "horse_racing", "horseshoes", "ice_hockey", "ice_stock", "judo", "karting", "korfball", "long_jump", "model_aerodrome", "motocross", "motor", "multi", "netball", "orienteering", "paddle_tennis", "paintball", "paragliding", "pelota", "racquet", "rc_car", "rowing", "rugby", "rugby_league", "rugby_union", "running", "sailing", "scuba_diving", "shooting", "shooting_range", "skateboard", "skating", "skiing", "soccer", "surfing", "swimming", "table_soccer", "table_tennis", "team_handball", "tennis", "toboggan", "volleyball", "water_ski", "yoga" },
					tourism = Set { "alpine_hut", "aquarium", "artwork", "attraction", "bed_and_breakfast", "camp_site", "caravan_site", "chalet", "gallery", "guest_house", "hostel", "hotel", "information", "motel", "museum", "picnic_site", "theme_park", "viewpoint", "zoo" },
					waterway = Set { "dock" } }

-- POI "class" values: based on https://github.com/openmaptiles/openmaptiles/blob/master/layers/poi/poi.yaml
poiClasses      = { townhall="town_hall", public_building="town_hall", courthouse="town_hall", community_centre="town_hall",
					golf="golf", golf_course="golf", miniature_golf="golf",
					fast_food="fast_food", food_court="fast_food",
					park="park", bbq="park",
					bus_stop="bus", bus_station="bus",
					subway_entrance="entrance", train_station_entrance="entrance",
					camp_site="campsite", caravan_site="campsite",
					laundry="laundry", dry_cleaning="laundry",
					supermarket="grocery", deli="grocery", delicatessen="grocery", department_store="grocery", greengrocer="grocery", marketplace="grocery",
					books="library", library="library",
					university="college", college="college",
					hotel="lodging", motel="lodging", bed_and_breakfast="lodging", guest_house="lodging", hostel="lodging", chalet="lodging", alpine_hut="lodging", dormitory="lodging",
					chocolate="ice_cream", confectionery="ice_cream",
					post_box="post",  post_office="post",
					cafe="cafe",
					school="school",  kindergarten="school",
					alcohol="alcohol_shop",  beverages="alcohol_shop",  wine="alcohol_shop",
					bar="bar", nightclub="bar",
					marina="harbor", dock="harbor",
					car="car", car_repair="car", taxi="car",
					hospital="hospital", nursing_home="hospital",  clinic="hospital",
					grave_yard="cemetery", cemetery="cemetery",
					attraction="attraction", viewpoint="attraction",
					biergarten="beer", pub="beer",
					music="music", musical_instrument="music",
					american_football="stadium", stadium="stadium", soccer="stadium",
					art="art_gallery", artwork="art_gallery", gallery="art_gallery", arts_centre="art_gallery",
					bag="clothing_store", clothes="clothing_store",
					swimming_area="swimming", swimming="swimming",
					castle="castle", ruins="castle" }
-- POI classes where class is the matching value and subclass is the value of a separate key
poiSubClasses = { information="information", place_of_worship="religion", pitch="sport" }
poiClassRanks   = { hospital=1, railway=2, bus=3, attraction=4, harbor=5, college=6,
					school=7, stadium=8, zoo=9, town_hall=10, campsite=11, cemetery=12,
					park=13, library=14, police=15, post=16, golf=17, shop=18, grocery=19,
					fast_food=20, clothing_store=21, bar=22 }
waterClasses    = Set { "river", "riverbank", "stream", "canal", "drain", "ditch", "dock" }
waterwayClasses = Set { "stream", "river", "canal", "drain", "ditch" }

-- Scan relations for use in ways

function relation_scan_function()
	if Find("type")=="boundary" and Find("boundary")=="administrative" then
		Accept()
	end
end

function write_to_transportation_layer(minzoom, highway_class)
	Layer("transportation", false)
	MinZoom(minzoom)
	SetZOrder()
	Attribute("class", highway_class)
	-- SLIM: stripped brunnel / ramp / service / oneway / surface / access /
	--       bicycle / foot / horse / toll / layer / expressway / mtb_scale.
	--       Our client style only filters on `class`.
end

-- Process way tags

function way_function()
	local route    = Find("route")
	local highway  = Find("highway")
	local waterway = Find("waterway")
	local water    = Find("water")
	local building = Find("building")
	local natural  = Find("natural")
	local historic = Find("historic")
	local landuse  = Find("landuse")
	local leisure  = Find("leisure")
	local amenity  = Find("amenity")
	local aeroway  = Find("aeroway")
	local railway  = Find("railway")
	local service  = Find("service")
	local sport    = Find("sport")
	local shop     = Find("shop")
	local tourism  = Find("tourism")
	local man_made = Find("man_made")
	local boundary = Find("boundary")
	local isClosed = IsClosed()
	local write_name = false
	local construction = Find("construction")

	-- Miscellaneous preprocessing
	if Find("disused") == "yes" then return end
	if boundary~="" and Find("protection_title")=="National Forest" and Find("operator")=="United States Forest Service" then return end
	if highway == "proposed" then return end
	if aerowayBuildings[aeroway] then building="yes"; aeroway="" end
	if landuse == "field" then landuse = "farmland" end
	if landuse == "meadow" and Find("meadow")=="agricultural" then landuse="farmland" end

	-- Boundaries within relations
	-- note that we process administrative boundaries as properties on ways, rather than as single relation geometries,
	--  because otherwise we get multiple renderings where boundaries are coterminous
	local admin_level = 11
	local isBoundary = false
	while true do
		local rel = NextRelation()
		if not rel then break end
		isBoundary = true
		admin_level = math.min(admin_level, tonumber(FindInRelation("admin_level")) or 11)
	end

	-- Boundaries in ways
	if boundary=="administrative" then
		admin_level = math.min(admin_level, tonumber(Find("admin_level")) or 11)
		isBoundary = true
	end

	-- Administrative boundaries
	-- SLIM: client only renders admin_level 2 (country) and 4 (state); skip
	--       all other levels (counties/cities/etc. are never shown).
	if isBoundary and not (Find("maritime")=="yes")
	   and (admin_level == 2 or admin_level == 4) then
		Layer("boundary",false)
		AttributeNumeric("admin_level", admin_level)
		MinZoom(admin_level == 2 and 0 or 4)
	end

	-- Roads ('transportation' and 'transportation_name', plus 'transportation_name_detail')
	if highway~="" then
		local access = Find("access")
		local surface = Find("surface")

		local h = highway
		local minzoom = 99
		local layer = "transportation"
		if majorRoadValues[highway] then              minzoom = 4 end
		if highway == "trunk"       then              minzoom = 5
		elseif highway == "primary" then              minzoom = 7 end
		if mainRoadValues[highway]  then              minzoom = 9 end
		if midRoadValues[highway]   then              minzoom = 11 end
		if minorRoadValues[highway] then h = "minor"; minzoom = 12 end
		if trackValues[highway]     then h = "track"; minzoom = 14 end
		if pathValues[highway]      then h = "path" ; minzoom = 14 end
		if h=="service"             then              minzoom = 12 end

		-- Links (ramp)
		local ramp=false
		if linkValues[highway] then
			splitHighway = split(highway, "_")
			highway = splitHighway[1]; h = highway
			ramp = true
			minzoom = 11
		end

		-- Construction
		if highway == "construction" then
			if constructionValues[construction] then
				h = construction .. "_construction"
				if construction ~= "service" and construction ~= "track" then
					minzoom = 11
				else
					minzoom = 12
				end
			else
				h = "minor_construction"
				minzoom = 14
			end
		end

		-- Write to layer
		if minzoom <= 14 then
			write_to_transportation_layer(minzoom, h)

			-- Write names
			if minzoom < 8 then
				minzoom = 8
			end
			if highway == "motorway" or highway == "trunk" then
				Layer("transportation_name", false)
				MinZoom(minzoom)
			elseif h == "minor" or h == "track" or h == "path" or h == "service" then
				Layer("transportation_name_detail", false)
				MinZoom(minzoom)
			else
				Layer("transportation_name_mid", false)
				MinZoom(minzoom)
			end
			SetNameAttributes()
			-- SLIM: stripped network / subclass / ref / ref_length (no highway
			--       shields) and class (unused by client style).
		end
	end

	-- Railways ('transportation' and 'transportation_name')
	if railway~="" then
		Layer("transportation", false)
		Attribute("class", railway)
		SetZOrder()
		if service~="" then MinZoom(12) else MinZoom(9) end
		-- SLIM: stripped brunnel and service attributes.

		Layer("transportation_name", false)
		SetNameAttributes()
		MinZoom(14)
		-- SLIM: stripped class attribute on rail names.
	end

	-- Pier
	if man_made=="pier" then
		Layer("transportation", isClosed)
		SetZOrder()
		Attribute("class", "pier")
		SetMinZoomByArea()
	end

	-- 'Ferry'
	if route=="ferry" then
		Layer("transportation", false)
		Attribute("class", "ferry")
		SetZOrder()
		MinZoom(9)
		-- SLIM: stripped brunnel.

		Layer("transportation_name", false)
		SetNameAttributes()
		MinZoom(12)
		-- SLIM: stripped class attribute on ferry names.
	end

	-- SLIM: `aeroway` (runway/taxiway lines) dropped.
	if aeroway~="" then
		-- write_name stays true so aerodrome_label still gets named.
		write_name = true
	end

	-- 'aerodrome_label'
	if aeroway=="aerodrome" then
	 	LayerAsCentroid("aerodrome_label")
	 	SetNameAttributes()
	 	Attribute("iata", Find("iata"))
  		SetEleAttributes()
 	 	Attribute("icao", Find("icao"))

 	 	local aerodrome = Find(aeroway)
 	 	local class
 	 	if aerodromeValues[aerodrome] then class = aerodrome else class = "other" end
 	 	Attribute("class", class)
	end

	-- SLIM: `waterway` line layer dropped (rivers/streams as lines).
	-- Closed waterways (boatyards, dams, locks) still fall through below.
	if waterwayClasses[waterway] and not isClosed then
		-- dropped
	elseif waterway == "boatyard"  then Layer("landuse", isClosed); Attribute("class", "industrial"); MinZoom(12)
	elseif waterway == "dam"       then Layer("building",isClosed)
	elseif waterway == "fuel"      then Layer("landuse", isClosed); Attribute("class", "industrial"); MinZoom(14)
	end
	-- Set names on rivers
	if waterwayClasses[waterway] and not isClosed then
		if waterway == "river" and Holds("name") then
			Layer("water_name", false)
		else
			Layer("water_name_detail", false)
			MinZoom(14)
		end
		Attribute("class", waterway)
		SetNameAttributes()
	end

	-- Set 'building' and associated
	if building~="" then
		Layer("building", true)
		-- SLIM: stripped SetBuildingHeightAttributes (render_height/min_height)
		--       — client style draws flat fills, no 3D extrusion.
		SetMinZoomByArea()
	end

	-- (Housenumbers on ways are no longer written to tiles; see
	-- build-housenumbers.py which extracts them into a dedicated bundle.)

	-- Set 'water'
	if natural=="water" or leisure=="swimming_pool" or landuse=="reservoir" or landuse=="basin" or waterClasses[waterway] then
		if Find("covered")=="yes" or not isClosed then return end
		local class="lake"; if waterway~="" then class="river" end
		if class=="lake" and Find("wikidata")=="Q192770" then return end
		Layer("water",true)
		SetMinZoomByArea(way)
		-- SLIM: stripped class / intermittent — client style is flat fill.
		-- we only want to show the names of actual lakes not every man-made basin that probably doesn't even have a name other than "basin"
		-- examples for which we don't want to show a name:
		--  https://www.openstreetmap.org/way/25958687
		--  https://www.openstreetmap.org/way/27201902
		--  https://www.openstreetmap.org/way/25309134
		--  https://www.openstreetmap.org/way/24579306
		if Holds("name") and natural=="water" and water ~= "basin" and water ~= "wastewater" then
			LayerAsCentroid("water_name_detail")
			SetNameAttributes()
			SetMinZoomByArea()
			-- SLIM: stripped class — client labels every water_name the same.
		end

		return -- in case we get any landuse processing
	end

	-- Set 'landcover' (from landuse, natural, leisure)
	local l = landuse
	if l=="" then l=natural end
	if l=="" then l=leisure end
	if landcoverKeys[l] then
		Layer("landcover", true)
		SetMinZoomByArea()
		-- SLIM: stripped class / subclass — client style is flat fill.
		write_name = true

	-- Set 'landuse'
	else
		if l=="" then l=amenity end
		if l=="" then l=tourism end
		if landuseKeys[l] then
			Layer("landuse", true)
			-- SLIM: stripped class — client style is flat fill.
			if l=="residential" then
				if Area()<ZRES8^2 then MinZoom(8)
				else SetMinZoomByArea() end
			else MinZoom(11) end
			write_name = true
		end
	end

	-- Parks — labelled by the 'park-labels' client style layer.
	if     boundary=="national_park" then Layer("park",true); SetNameAttributes()
	elseif leisure=="nature_reserve" then Layer("park",true); SetNameAttributes() end

	-- POIs ('poi' and 'poi_detail')
	local rank, class, subclass = GetPOIRank()
	if rank then WritePOI(class,subclass,rank); return end

	-- Catch-all — SLIM: client doesn't filter POIs by rank; skip rank attr.
	if (building~="" or write_name) and Holds("name") then
		LayerAsCentroid("poi_detail")
		SetNameAttributes()
	end
end

-- Remap shapefile feature attributes to vector-tile attributes.
function attribute_function(attr,layer)
	if attr["featurecla"]=="Glaciated areas" then
		return { subclass="glacier" }
	elseif attr["featurecla"]=="Antarctic Ice Shelf" then
		return { subclass="ice_shelf" }
	elseif attr["featurecla"]=="Urban area" then
		return { class="residential" }
	elseif attr["featurecla"]=="Land" then
		-- ne_10m_land: plain continent polygons, written to the landcover
		-- layer so the client fills them with the standard land colour at
		-- low zoom.
		return { subclass="land" }
	elseif attr["featurecla"]=="Ocean" then
		-- ne_10m_ocean: world-wide ocean polygon, written to the water layer
		-- so low-zoom views show blue behind the continents.
		return { class="ocean" }
	else
		return { class="ocean" }
	end
end

-- ==========================================================
-- Common functions

-- Write a way centroid to POI layer
function WritePOI(class,subclass,rank)
	local layer = "poi"
	if rank>4 then layer="poi_detail" end
	LayerAsCentroid(layer)
	SetNameAttributes()
	Attribute("class", class)
	Attribute("subclass", subclass)
	-- SLIM: stripped rank / layer / indoor / level — client style uses only
	--       class + subclass (for icon lookup) and name:latin (for label).
end

-- Set name attributes on any object
function SetNameAttributes()
	local name = Find("name"), iname
	local main_written = name
	-- if we have a preferred language, then write that (if available), and additionally write the base name tag
	if preferred_language and Holds("name:"..preferred_language) then
		iname = Find("name:"..preferred_language)
		Attribute(preferred_language_attribute, iname)
		if iname~=name and default_language_attribute then
			Attribute(default_language_attribute, name)
		else main_written = iname end
	else
		Attribute(preferred_language_attribute, name)
	end
	-- then set any additional languages
	for i,lang in ipairs(additional_languages) do
		iname = Find("name:"..lang)
		if iname=="" then iname=name end
		if iname~=main_written then Attribute("name:"..lang, iname) end
	end
end

-- Set ele and ele_ft on any object
function SetEleAttributes()
    local ele = Find("ele")
	if ele ~= "" then
		local meter = math.floor(tonumber(ele) or 0)
		local feet = math.floor(meter * 3.2808399)
		AttributeNumeric("ele", meter)
		AttributeNumeric("ele_ft", feet)
    end
end

function SetBrunnelAttributes()
	if     Find("bridge") == "yes" then Attribute("brunnel", "bridge")
	elseif Find("tunnel") == "yes" then Attribute("brunnel", "tunnel")
	elseif Find("ford")   == "yes" then Attribute("brunnel", "ford")
	end
end

-- Set minimum zoom level by area
function SetMinZoomByArea()
	local area=Area()
	if     area>ZRES5^2  then MinZoom(6)
	elseif area>ZRES6^2  then MinZoom(7)
	elseif area>ZRES7^2  then MinZoom(8)
	elseif area>ZRES8^2  then MinZoom(9)
	elseif area>ZRES9^2  then MinZoom(10)
	elseif area>ZRES10^2 then MinZoom(11)
	elseif area>ZRES11^2 then MinZoom(12)
	elseif area>ZRES12^2 then MinZoom(13)
	else                      MinZoom(14) end
end

-- Calculate POIs (typically rank 1-4 go to 'poi' z12-14, rank 5+ to 'poi_detail' z14)
-- returns rank, class, subclass
function GetPOIRank()
	local k,list,v,class,rank

	-- Can we find the tag?
	for k,list in pairs(poiTags) do
		if list[Find(k)] then
			v = Find(k)	-- k/v are the OSM tag pair
			class = poiClasses[v] or k
			rank  = poiClassRanks[class] or 25
			subclassKey = poiSubClasses[v]
			if subclassKey then
				class = v
				v = Find(subclassKey)
			end
			return rank, class, v
		end
	end

	-- Catch-all for shops
	local shop = Find("shop")
	if shop~="" then return poiClassRanks['shop'], "shop", shop end

	-- Nothing found
	return nil,nil,nil
end

function SetBuildingHeightAttributes()
	local height = tonumber(Find("height"), 10)
	local minHeight = tonumber(Find("min_height"), 10)
	local levels = tonumber(Find("building:levels"), 10)
	local minLevel = tonumber(Find("building:min_level"), 10)

	local renderHeight = BUILDING_FLOOR_HEIGHT
	if height or levels then
		renderHeight = height or (levels * BUILDING_FLOOR_HEIGHT)
	end
	local renderMinHeight = 0
	if minHeight or minLevel then
		renderMinHeight = minHeight or (minLevel * BUILDING_FLOOR_HEIGHT)
	end

	-- Fix upside-down buildings
	if renderHeight < renderMinHeight then
		renderHeight = renderHeight + renderMinHeight
	end

	AttributeNumeric("render_height", renderHeight)
	AttributeNumeric("render_min_height", renderMinHeight)
end

-- Implement z_order as calculated by Imposm
-- See https://imposm.org/docs/imposm3/latest/mapping.html#wayzorder for details.
function SetZOrder()
	local highway = Find("highway")
	local layer = tonumber(Find("layer"))
	local bridge = Find("bridge")
	local tunnel = Find("tunnel")
	local zOrder = 0
	if bridge ~= "" and bridge ~= "no" then
		zOrder = zOrder + 10
	elseif tunnel ~= "" and tunnel ~= "no" then
		zOrder = zOrder - 10
	end
	if not (layer == nil) then
		if layer > 7 then
			layer = 7
		elseif layer < -7 then
			layer = -7
		end
		zOrder = zOrder + layer * 10
	end
	local hwClass = 0
	-- See https://github.com/omniscale/imposm3/blob/53bb80726ca9456e4a0857b38803f9ccfe8e33fd/mapping/columns.go#L251
	if highway == "motorway" then
		hwClass = 9
	elseif highway == "trunk" then
		hwClass = 8
	elseif highway == "primary" then
		hwClass = 6
	elseif highway == "secondary" then
		hwClass = 5
	elseif highway == "tertiary" then
		hwClass = 4
	else
		hwClass = 3
	end
	zOrder = zOrder + hwClass
	ZOrder(zOrder)
end

-- ==========================================================
-- Lua utility functions

function split(inputstr, sep) -- https://stackoverflow.com/a/7615129/4288232
	if sep == nil then
		sep = "%s"
	end
	local t={} ; i=1
	for str in string.gmatch(inputstr, "([^"..sep.."]+)") do
		t[i] = str
		i = i + 1
	end
	return t
end

-- vim: tabstop=2 shiftwidth=2 noexpandtab
