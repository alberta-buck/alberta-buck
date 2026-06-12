// SPDX-License-Identifier: GPL-3.0
/*
    Copyright 2021 0KIMS association.

    This file is generated with [snarkJS](https://github.com/iden3/snarkjs).

    snarkJS is a free software: you can redistribute it and/or modify it
    under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    snarkJS is distributed in the hope that it will be useful, but WITHOUT
    ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
    or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public
    License for more details.

    You should have received a copy of the GNU General Public License
    along with snarkJS. If not, see <https://www.gnu.org/licenses/>.
*/

pragma solidity >=0.7.0 <0.9.0;

contract MintBatchN32Groth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 709997778838307835264383870894323768967370809777328976677908306577656761867;
    uint256 constant alphay  = 12836297161486382489279136251040704944281163072493113933910361141757593083677;
    uint256 constant betax1  = 3684114477087397643340155885713584867253826796058431906225088186474534956028;
    uint256 constant betax2  = 21569416133113965215801657701571922097548973573516190817471638180089358098266;
    uint256 constant betay1  = 4326734521026516125659375800180265709604472291722677762145538662848384222069;
    uint256 constant betay2  = 16828383835347505433443117675638127444543738572256217033768785829213672209398;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 17057675015589608930122424840072316644058026698939402095273568425711170135886;
    uint256 constant deltax2 = 11111088907784108214927920808742391113801113446984843954808421701153531626163;
    uint256 constant deltay1 = 16547716083497184023217607346911935185631882755825053806166852793373330865946;
    uint256 constant deltay2 = 8448810368986796486955814113802234513705503598068541453131005088510673797205;

    
    uint256 constant IC0x = 17956582290763973378891035295407251433697807898925943552432783422315311321093;
    uint256 constant IC0y = 3621335170698282156796645639114915459232339682644183419519406937186311278828;
    
    uint256 constant IC1x = 17520461432048811049743558807922956010755549579919812339672492933632465942711;
    uint256 constant IC1y = 13394716909690925758905269429112266879837241673171469567409206116587871355963;
    
    uint256 constant IC2x = 7866059379776840525305772020377387442541294079236946830882733530399809402099;
    uint256 constant IC2y = 3658404828019984811072694979435666389354179509213361618555724452257772099218;
    
    uint256 constant IC3x = 15971840680011493898576925046613268851227181563181719576853597087370503055344;
    uint256 constant IC3y = 4348167724295343712955152121743846235116608821955294883819400335023115526211;
    
    uint256 constant IC4x = 15928390867927868001757351706161393811406024829552260658840525035143321865997;
    uint256 constant IC4y = 17057202766017467114754269202106783530893383247380017410907909334630225533751;
    
    uint256 constant IC5x = 19744920545568491567664054434477041742942405631042244888146363524948189905902;
    uint256 constant IC5y = 1590488332575232493736546154688331053538082570483478442969875785251280298557;
    
    uint256 constant IC6x = 16501628934483066822118734796577476017475989236123850144118398289687732682048;
    uint256 constant IC6y = 16443592993263338983859951957400327528803811244946954244083804822752667193109;
    
    uint256 constant IC7x = 18529454298307567461520105422490716928254496181226795155935699548144935709075;
    uint256 constant IC7y = 9281229189287828503752681116655131984911558232170336993536946285169532099633;
    
    uint256 constant IC8x = 18417028739873989541765970011989819510726639676932694239897721497957395461477;
    uint256 constant IC8y = 2397882998677233389579286726787717092267696799824003758983753701829180469167;
    
    uint256 constant IC9x = 19824406362242365681692984320597073173522348526789128637233228076168606677401;
    uint256 constant IC9y = 8914934615041639178035833064227779502294904081761898501172596415058645833752;
    
    uint256 constant IC10x = 18788393188477858784995640450532364343542039215253056201415274002741373504249;
    uint256 constant IC10y = 9370685852398956215458197215189063011731402139611135616332163059025798638825;
    
    uint256 constant IC11x = 6430207440004323042533257206857869898095391045596527574809052184579387475004;
    uint256 constant IC11y = 10709983411563035061245046471909523790020387451705723055220781124672344068426;
    
    uint256 constant IC12x = 14966927574716123908507244944358048841479994367618955765848334295500373888222;
    uint256 constant IC12y = 19421719515482521321771267052731596140032883990420056899839326719325413833893;
    
    uint256 constant IC13x = 12245812792489819109602881747007616574546249884873340085477926644918594663690;
    uint256 constant IC13y = 9762087524314905387776026264672495858099793110522654688515550586422522533024;
    
    uint256 constant IC14x = 11557054280221120567415900070600320101814826227522231666089167664153418805334;
    uint256 constant IC14y = 1193926129269177055717233041148556816932219664122742722635111865882502273766;
    
    uint256 constant IC15x = 1403052533826203247929864160683974306639968495917886943508973355352099130161;
    uint256 constant IC15y = 3643070505969121225775863273809679187735116039913065325547457428210985083524;
    
    uint256 constant IC16x = 15861939913150734773204412990398500638622780909392058616456672232378615781751;
    uint256 constant IC16y = 19159520129830609054075608397922521691596788896577784621067643844135835750862;
    
    uint256 constant IC17x = 8432820709923898383642452982114421645210487027187415560198645391951695751971;
    uint256 constant IC17y = 17747224178380475967494854332793865120957805543930755918921615579491298220433;
    
    uint256 constant IC18x = 636476240381657737974939816525823480815930429708479205819294767701782701593;
    uint256 constant IC18y = 15716414883746489293966860442372278480881469492747707838040888124772821806815;
    
    uint256 constant IC19x = 21294809557572530131258577402106617278628999797879995364848340194772752095730;
    uint256 constant IC19y = 4951619057079689418953216524366759661520092362928657301543938549436183364762;
    
    uint256 constant IC20x = 10662078678305305300046499817159716173826175620281541429603908210024811375475;
    uint256 constant IC20y = 7398539548950879950839432614613676293705686334500745649873054567075871565995;
    
    uint256 constant IC21x = 2720682419614082134507287284226377483032842224934812939775012737796717169815;
    uint256 constant IC21y = 11529948515546008376096718435548851883435069567503964977232273055143278634721;
    
    uint256 constant IC22x = 12649983017320623913882790915135782922804158189557326000587934357366795766058;
    uint256 constant IC22y = 11659794279292962348469432446078382531367679294939818870011165381646702088335;
    
    uint256 constant IC23x = 1467704963762156711320338183747458680287580778154364748476822233688135643937;
    uint256 constant IC23y = 5639207387837843245280214359037495428637925423878682590789681457739699864397;
    
    uint256 constant IC24x = 17979333721683454055797778280174849155378492538075878999195309816552297178091;
    uint256 constant IC24y = 18226481715367120956401565637826442304341609335558148638983534517843922209702;
    
    uint256 constant IC25x = 1751118634805632051319782787195341407839704655088497314442071868963968246073;
    uint256 constant IC25y = 10649750850947277962436553804749271345247389828816162433438953586424342771199;
    
    uint256 constant IC26x = 19256730593112903973402014693508522724404927375106469909414773460850994323626;
    uint256 constant IC26y = 9258112830018642064654581177198753936621238988381407232139827371826992789927;
    
    uint256 constant IC27x = 4898717771097632091376625749316729856236363464518437589071106115570424865175;
    uint256 constant IC27y = 21009554910569615337769897259881466902386650465310019739093779457675441469650;
    
    uint256 constant IC28x = 5315537250423609146973485123128926182541666098236031697616669948809257002687;
    uint256 constant IC28y = 15380483384728571889975410929610836036241380200545479425898034364401378421747;
    
    uint256 constant IC29x = 14519486595228113279866098825126245100771712364433695865502541627782354976244;
    uint256 constant IC29y = 16584639207003034329268264282443565647219846913580669608030456119546308881950;
    
    uint256 constant IC30x = 9493475596420274842082535290826860001469597888280074654622935095393301146115;
    uint256 constant IC30y = 11081545371795254093729853564510677431259904516430713730340580169771738266548;
    
    uint256 constant IC31x = 11519261012752006757754844379011323307433853020137593393705150964750595876233;
    uint256 constant IC31y = 8646441041986489429163385789233404008676423679779225330068060088539461654246;
    
    uint256 constant IC32x = 5129320150473044964249775239218548749634132489449571098357210692339916205096;
    uint256 constant IC32y = 15153480583205436403482267245197536488489644846781774069279771688746103808112;
    
    uint256 constant IC33x = 11345187102767026163966058464549212744876537204631293697424297103687992216784;
    uint256 constant IC33y = 8908019329805483288556752926347303900074908960565979508688354616750401045897;
    
    uint256 constant IC34x = 21420980798534689321162911200258040531803634197740543243802337694411007083077;
    uint256 constant IC34y = 3313529793762110939413390020246797109502114770795746175862740969733537640880;
    
    uint256 constant IC35x = 20952623387428797514066430467110532967827504029735045209170717811958869079819;
    uint256 constant IC35y = 1929246040876961490686221681272490668861343602690202599441490205041639619300;
    
    uint256 constant IC36x = 7856308420158559990153258589335402278718208811645918557291081126436777248181;
    uint256 constant IC36y = 20459421000080887432077829000844927314387189396050058582653094343636558876596;
    
    uint256 constant IC37x = 10253506872799128090274528489335995357087244900243745782659330936830657447029;
    uint256 constant IC37y = 6462722511932751780431164055614541345687193447660380401587092102831925402604;
    
    uint256 constant IC38x = 687529157823624041525372494465616410531390397265630343486897271126252512288;
    uint256 constant IC38y = 6296229256662124065696700015006610319918867405090131710139823582292468620873;
    
    uint256 constant IC39x = 1957329547476064237834278463595420008471438938209207471738931796094071554642;
    uint256 constant IC39y = 15371007661619708367034981261533679040811976362674939755872970180542794558818;
    
    uint256 constant IC40x = 21138640494179431342480885761771440664539972895028236403328172825268445892950;
    uint256 constant IC40y = 15095765924202251218805183812352125660429395493247188175038619577795558694204;
    
    uint256 constant IC41x = 6199636549427290664055376002303917852873647697658647870785337323696347853718;
    uint256 constant IC41y = 12379980505424847987439974902626362900489248554862551913448308594240411496423;
    
    uint256 constant IC42x = 4674504702240612869673181787639763425301247849534005019908049297757380066380;
    uint256 constant IC42y = 6797179008119733359745348264405617599003465161877330612243567903073973328145;
    
    uint256 constant IC43x = 12960922067590449520365612056883505299603523476673015008503318255715350170112;
    uint256 constant IC43y = 15579871698415601977393886789483160016295787479312111977535387237555147008132;
    
    uint256 constant IC44x = 15660556362453208579921713329742696030034642787143261554294326894140930978795;
    uint256 constant IC44y = 11788922616892906999590716032669905351989167715198841799324211738273942343957;
    
    uint256 constant IC45x = 17247280729585766444992911053985314922717157965825944540705490517983186886132;
    uint256 constant IC45y = 16225641660849485998942608497655096653235632824434829459339618627810267527799;
    
    uint256 constant IC46x = 20858018169305731223934261660365764255090936013817066761459951180437501920622;
    uint256 constant IC46y = 15504443999325889027116516817438387673641871674402390716615820647996957655037;
    
    uint256 constant IC47x = 9082262744195990971040704263069097155384778804973643761055361889892529224000;
    uint256 constant IC47y = 653809145146766676644535971735296296856606698633180795952087245816102718583;
    
    uint256 constant IC48x = 21184972825193667112422680179346695698237547116164507461500276733772708702247;
    uint256 constant IC48y = 9614135185300483546884973871825391208916701067591930736831116253786242517901;
    
    uint256 constant IC49x = 16251498442364972658422586798060995203473082237330189466694745981978172027411;
    uint256 constant IC49y = 19566430098534257445830384471667188848355222216900149865297866573017559215820;
    
    uint256 constant IC50x = 19016143211555220278918813326537931089085020863463361511323791180012593933247;
    uint256 constant IC50y = 10606921222884299466911193305791194407815870429831303502085111072642893837435;
    
    uint256 constant IC51x = 11383381672984885380454383035277883052182551221689877982185797385767704939196;
    uint256 constant IC51y = 19884430689932509202349925856015068997925523806764624911756866718760303809399;
    
    uint256 constant IC52x = 8316845885298438330973555915812923199658520079963895420749467952358783592859;
    uint256 constant IC52y = 17687154643559715854833970538069567713421218548706693336645855848348816813334;
    
    uint256 constant IC53x = 14497380462654093015792322846891202112090397293316443202392855530888524473946;
    uint256 constant IC53y = 21086892645887697938120548334259123637544312441045534395318730019834872631907;
    
    uint256 constant IC54x = 180814061136882378934526101201950705394019587415113988356581229224762143460;
    uint256 constant IC54y = 10646258654461423821845109568308310576454748305355915603614759919630115706367;
    
    uint256 constant IC55x = 12563783237852832738852659689593060174488907650379700203175244575990247119689;
    uint256 constant IC55y = 6982655516175303989050821063236616960230042298593559010029537683237192311638;
    
    uint256 constant IC56x = 3999169474978204028298114222012872623390670831833868450294216923392314396517;
    uint256 constant IC56y = 20876653314977711866074696916155437978426038324056699923919254389351232710643;
    
    uint256 constant IC57x = 20081193014254852416319691790715616269062027700091061305440719590675502553839;
    uint256 constant IC57y = 11301646843699344960593616102694882892243121083490631045357395299110944503198;
    
    uint256 constant IC58x = 14634811383848350051644475850581201334887529416673668941161615112729936538520;
    uint256 constant IC58y = 9933708937147878035668243471410920950416073779171325767495897212595770361487;
    
    uint256 constant IC59x = 7419255343037163949877189388654344721265511556831411778518634553524710215310;
    uint256 constant IC59y = 3015887931396727014206383191029623492349145130086883640140705869282282022314;
    
    uint256 constant IC60x = 9512110538191480993736030671009443280232255746702165073787159294981174472272;
    uint256 constant IC60y = 12606059929874143107738252778025291225080782403117711557075383977803431645544;
    
    uint256 constant IC61x = 4422867424471588827455415164944774227195234497438367152813251457345575530336;
    uint256 constant IC61y = 17277064015822170748669808394073699788865546580721660733500826445463802829487;
    
    uint256 constant IC62x = 5963049441449872234679555753700254624685783295249639165647829669738180764774;
    uint256 constant IC62y = 19138125398053300013716513170496399388815058487106974958370728554504233843702;
    
    uint256 constant IC63x = 17507039063091130698251041830985467251012809174878660619936348763705479644254;
    uint256 constant IC63y = 6052780573199091820898801700494777044818841525639976625390568277676744725460;
    
    uint256 constant IC64x = 20349664970548439420775537398483276441887977854811786546434798918107685177827;
    uint256 constant IC64y = 5875679054329103582383932915143833087941624387119583286034837718019575541694;
    
    uint256 constant IC65x = 1816190438438499565213885729406982444648102619796918163435894386727001018638;
    uint256 constant IC65y = 16650825496744952490107307217374060676535331650160871693900394438187556061772;
    
    uint256 constant IC66x = 1568969843079903444261639159377378969143699005916909696642715145091263471905;
    uint256 constant IC66y = 60202432440161997663131705326081124439243949800964197598987386209623836586;
    
    uint256 constant IC67x = 15233755844362912491431847926735606524700217522815361149264820576012427289852;
    uint256 constant IC67y = 8271853171055757525581624315621839403443285297204168798361806357700564628180;
    
    uint256 constant IC68x = 10400435551699137537269345964634252267648499063541825193908996482877535068830;
    uint256 constant IC68y = 15049715558591223928335251286207027042145766619235028460612221936186762595580;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[68] calldata _pubSignals) public returns (bool) {
        assembly {
            function checkField(v) {
                if iszero(lt(v, r)) {
                    mstore(0, 0)
                    return(0, 0x20)
                }
            }
            
            // G1 function to multiply a G1 value(x,y) to value in an address
            function g1_mulAccC(pR, x, y, s) {
                let success
                let mIn := mload(0x40)
                mstore(mIn, x)
                mstore(add(mIn, 32), y)
                mstore(add(mIn, 64), s)

                success := staticcall(sub(gas(), 2000), 7, mIn, 96, mIn, 64)

                if iszero(success) {
                    mstore(0, 0)
                    return(0, 0x20)
                }

                mstore(add(mIn, 64), mload(pR))
                mstore(add(mIn, 96), mload(add(pR, 32)))

                success := staticcall(sub(gas(), 2000), 6, mIn, 128, pR, 64)

                if iszero(success) {
                    mstore(0, 0)
                    return(0, 0x20)
                }
            }

            function checkPairing(pA, pB, pC, pubSignals, pMem) -> isOk {
                let _pPairing := add(pMem, pPairing)
                let _pVk := add(pMem, pVk)

                mstore(_pVk, IC0x)
                mstore(add(_pVk, 32), IC0y)

                // Compute the linear combination vk_x
                
                g1_mulAccC(_pVk, IC1x, IC1y, calldataload(add(pubSignals, 0)))
                
                g1_mulAccC(_pVk, IC2x, IC2y, calldataload(add(pubSignals, 32)))
                
                g1_mulAccC(_pVk, IC3x, IC3y, calldataload(add(pubSignals, 64)))
                
                g1_mulAccC(_pVk, IC4x, IC4y, calldataload(add(pubSignals, 96)))
                
                g1_mulAccC(_pVk, IC5x, IC5y, calldataload(add(pubSignals, 128)))
                
                g1_mulAccC(_pVk, IC6x, IC6y, calldataload(add(pubSignals, 160)))
                
                g1_mulAccC(_pVk, IC7x, IC7y, calldataload(add(pubSignals, 192)))
                
                g1_mulAccC(_pVk, IC8x, IC8y, calldataload(add(pubSignals, 224)))
                
                g1_mulAccC(_pVk, IC9x, IC9y, calldataload(add(pubSignals, 256)))
                
                g1_mulAccC(_pVk, IC10x, IC10y, calldataload(add(pubSignals, 288)))
                
                g1_mulAccC(_pVk, IC11x, IC11y, calldataload(add(pubSignals, 320)))
                
                g1_mulAccC(_pVk, IC12x, IC12y, calldataload(add(pubSignals, 352)))
                
                g1_mulAccC(_pVk, IC13x, IC13y, calldataload(add(pubSignals, 384)))
                
                g1_mulAccC(_pVk, IC14x, IC14y, calldataload(add(pubSignals, 416)))
                
                g1_mulAccC(_pVk, IC15x, IC15y, calldataload(add(pubSignals, 448)))
                
                g1_mulAccC(_pVk, IC16x, IC16y, calldataload(add(pubSignals, 480)))
                
                g1_mulAccC(_pVk, IC17x, IC17y, calldataload(add(pubSignals, 512)))
                
                g1_mulAccC(_pVk, IC18x, IC18y, calldataload(add(pubSignals, 544)))
                
                g1_mulAccC(_pVk, IC19x, IC19y, calldataload(add(pubSignals, 576)))
                
                g1_mulAccC(_pVk, IC20x, IC20y, calldataload(add(pubSignals, 608)))
                
                g1_mulAccC(_pVk, IC21x, IC21y, calldataload(add(pubSignals, 640)))
                
                g1_mulAccC(_pVk, IC22x, IC22y, calldataload(add(pubSignals, 672)))
                
                g1_mulAccC(_pVk, IC23x, IC23y, calldataload(add(pubSignals, 704)))
                
                g1_mulAccC(_pVk, IC24x, IC24y, calldataload(add(pubSignals, 736)))
                
                g1_mulAccC(_pVk, IC25x, IC25y, calldataload(add(pubSignals, 768)))
                
                g1_mulAccC(_pVk, IC26x, IC26y, calldataload(add(pubSignals, 800)))
                
                g1_mulAccC(_pVk, IC27x, IC27y, calldataload(add(pubSignals, 832)))
                
                g1_mulAccC(_pVk, IC28x, IC28y, calldataload(add(pubSignals, 864)))
                
                g1_mulAccC(_pVk, IC29x, IC29y, calldataload(add(pubSignals, 896)))
                
                g1_mulAccC(_pVk, IC30x, IC30y, calldataload(add(pubSignals, 928)))
                
                g1_mulAccC(_pVk, IC31x, IC31y, calldataload(add(pubSignals, 960)))
                
                g1_mulAccC(_pVk, IC32x, IC32y, calldataload(add(pubSignals, 992)))
                
                g1_mulAccC(_pVk, IC33x, IC33y, calldataload(add(pubSignals, 1024)))
                
                g1_mulAccC(_pVk, IC34x, IC34y, calldataload(add(pubSignals, 1056)))
                
                g1_mulAccC(_pVk, IC35x, IC35y, calldataload(add(pubSignals, 1088)))
                
                g1_mulAccC(_pVk, IC36x, IC36y, calldataload(add(pubSignals, 1120)))
                
                g1_mulAccC(_pVk, IC37x, IC37y, calldataload(add(pubSignals, 1152)))
                
                g1_mulAccC(_pVk, IC38x, IC38y, calldataload(add(pubSignals, 1184)))
                
                g1_mulAccC(_pVk, IC39x, IC39y, calldataload(add(pubSignals, 1216)))
                
                g1_mulAccC(_pVk, IC40x, IC40y, calldataload(add(pubSignals, 1248)))
                
                g1_mulAccC(_pVk, IC41x, IC41y, calldataload(add(pubSignals, 1280)))
                
                g1_mulAccC(_pVk, IC42x, IC42y, calldataload(add(pubSignals, 1312)))
                
                g1_mulAccC(_pVk, IC43x, IC43y, calldataload(add(pubSignals, 1344)))
                
                g1_mulAccC(_pVk, IC44x, IC44y, calldataload(add(pubSignals, 1376)))
                
                g1_mulAccC(_pVk, IC45x, IC45y, calldataload(add(pubSignals, 1408)))
                
                g1_mulAccC(_pVk, IC46x, IC46y, calldataload(add(pubSignals, 1440)))
                
                g1_mulAccC(_pVk, IC47x, IC47y, calldataload(add(pubSignals, 1472)))
                
                g1_mulAccC(_pVk, IC48x, IC48y, calldataload(add(pubSignals, 1504)))
                
                g1_mulAccC(_pVk, IC49x, IC49y, calldataload(add(pubSignals, 1536)))
                
                g1_mulAccC(_pVk, IC50x, IC50y, calldataload(add(pubSignals, 1568)))
                
                g1_mulAccC(_pVk, IC51x, IC51y, calldataload(add(pubSignals, 1600)))
                
                g1_mulAccC(_pVk, IC52x, IC52y, calldataload(add(pubSignals, 1632)))
                
                g1_mulAccC(_pVk, IC53x, IC53y, calldataload(add(pubSignals, 1664)))
                
                g1_mulAccC(_pVk, IC54x, IC54y, calldataload(add(pubSignals, 1696)))
                
                g1_mulAccC(_pVk, IC55x, IC55y, calldataload(add(pubSignals, 1728)))
                
                g1_mulAccC(_pVk, IC56x, IC56y, calldataload(add(pubSignals, 1760)))
                
                g1_mulAccC(_pVk, IC57x, IC57y, calldataload(add(pubSignals, 1792)))
                
                g1_mulAccC(_pVk, IC58x, IC58y, calldataload(add(pubSignals, 1824)))
                
                g1_mulAccC(_pVk, IC59x, IC59y, calldataload(add(pubSignals, 1856)))
                
                g1_mulAccC(_pVk, IC60x, IC60y, calldataload(add(pubSignals, 1888)))
                
                g1_mulAccC(_pVk, IC61x, IC61y, calldataload(add(pubSignals, 1920)))
                
                g1_mulAccC(_pVk, IC62x, IC62y, calldataload(add(pubSignals, 1952)))
                
                g1_mulAccC(_pVk, IC63x, IC63y, calldataload(add(pubSignals, 1984)))
                
                g1_mulAccC(_pVk, IC64x, IC64y, calldataload(add(pubSignals, 2016)))
                
                g1_mulAccC(_pVk, IC65x, IC65y, calldataload(add(pubSignals, 2048)))
                
                g1_mulAccC(_pVk, IC66x, IC66y, calldataload(add(pubSignals, 2080)))
                
                g1_mulAccC(_pVk, IC67x, IC67y, calldataload(add(pubSignals, 2112)))
                
                g1_mulAccC(_pVk, IC68x, IC68y, calldataload(add(pubSignals, 2144)))
                

                // -A
                mstore(_pPairing, calldataload(pA))
                mstore(add(_pPairing, 32), mod(sub(q, calldataload(add(pA, 32))), q))

                // B
                mstore(add(_pPairing, 64), calldataload(pB))
                mstore(add(_pPairing, 96), calldataload(add(pB, 32)))
                mstore(add(_pPairing, 128), calldataload(add(pB, 64)))
                mstore(add(_pPairing, 160), calldataload(add(pB, 96)))

                // alpha1
                mstore(add(_pPairing, 192), alphax)
                mstore(add(_pPairing, 224), alphay)

                // beta2
                mstore(add(_pPairing, 256), betax1)
                mstore(add(_pPairing, 288), betax2)
                mstore(add(_pPairing, 320), betay1)
                mstore(add(_pPairing, 352), betay2)

                // vk_x
                mstore(add(_pPairing, 384), mload(add(pMem, pVk)))
                mstore(add(_pPairing, 416), mload(add(pMem, add(pVk, 32))))


                // gamma2
                mstore(add(_pPairing, 448), gammax1)
                mstore(add(_pPairing, 480), gammax2)
                mstore(add(_pPairing, 512), gammay1)
                mstore(add(_pPairing, 544), gammay2)

                // C
                mstore(add(_pPairing, 576), calldataload(pC))
                mstore(add(_pPairing, 608), calldataload(add(pC, 32)))

                // delta2
                mstore(add(_pPairing, 640), deltax1)
                mstore(add(_pPairing, 672), deltax2)
                mstore(add(_pPairing, 704), deltay1)
                mstore(add(_pPairing, 736), deltay2)


                let success := staticcall(sub(gas(), 2000), 8, _pPairing, 768, _pPairing, 0x20)

                isOk := and(success, mload(_pPairing))
            }

            let pMem := mload(0x40)
            mstore(0x40, add(pMem, pLastMem))

            // Validate that all evaluations ∈ F
            
            checkField(calldataload(add(_pubSignals, 0)))
            
            checkField(calldataload(add(_pubSignals, 32)))
            
            checkField(calldataload(add(_pubSignals, 64)))
            
            checkField(calldataload(add(_pubSignals, 96)))
            
            checkField(calldataload(add(_pubSignals, 128)))
            
            checkField(calldataload(add(_pubSignals, 160)))
            
            checkField(calldataload(add(_pubSignals, 192)))
            
            checkField(calldataload(add(_pubSignals, 224)))
            
            checkField(calldataload(add(_pubSignals, 256)))
            
            checkField(calldataload(add(_pubSignals, 288)))
            
            checkField(calldataload(add(_pubSignals, 320)))
            
            checkField(calldataload(add(_pubSignals, 352)))
            
            checkField(calldataload(add(_pubSignals, 384)))
            
            checkField(calldataload(add(_pubSignals, 416)))
            
            checkField(calldataload(add(_pubSignals, 448)))
            
            checkField(calldataload(add(_pubSignals, 480)))
            
            checkField(calldataload(add(_pubSignals, 512)))
            
            checkField(calldataload(add(_pubSignals, 544)))
            
            checkField(calldataload(add(_pubSignals, 576)))
            
            checkField(calldataload(add(_pubSignals, 608)))
            
            checkField(calldataload(add(_pubSignals, 640)))
            
            checkField(calldataload(add(_pubSignals, 672)))
            
            checkField(calldataload(add(_pubSignals, 704)))
            
            checkField(calldataload(add(_pubSignals, 736)))
            
            checkField(calldataload(add(_pubSignals, 768)))
            
            checkField(calldataload(add(_pubSignals, 800)))
            
            checkField(calldataload(add(_pubSignals, 832)))
            
            checkField(calldataload(add(_pubSignals, 864)))
            
            checkField(calldataload(add(_pubSignals, 896)))
            
            checkField(calldataload(add(_pubSignals, 928)))
            
            checkField(calldataload(add(_pubSignals, 960)))
            
            checkField(calldataload(add(_pubSignals, 992)))
            
            checkField(calldataload(add(_pubSignals, 1024)))
            
            checkField(calldataload(add(_pubSignals, 1056)))
            
            checkField(calldataload(add(_pubSignals, 1088)))
            
            checkField(calldataload(add(_pubSignals, 1120)))
            
            checkField(calldataload(add(_pubSignals, 1152)))
            
            checkField(calldataload(add(_pubSignals, 1184)))
            
            checkField(calldataload(add(_pubSignals, 1216)))
            
            checkField(calldataload(add(_pubSignals, 1248)))
            
            checkField(calldataload(add(_pubSignals, 1280)))
            
            checkField(calldataload(add(_pubSignals, 1312)))
            
            checkField(calldataload(add(_pubSignals, 1344)))
            
            checkField(calldataload(add(_pubSignals, 1376)))
            
            checkField(calldataload(add(_pubSignals, 1408)))
            
            checkField(calldataload(add(_pubSignals, 1440)))
            
            checkField(calldataload(add(_pubSignals, 1472)))
            
            checkField(calldataload(add(_pubSignals, 1504)))
            
            checkField(calldataload(add(_pubSignals, 1536)))
            
            checkField(calldataload(add(_pubSignals, 1568)))
            
            checkField(calldataload(add(_pubSignals, 1600)))
            
            checkField(calldataload(add(_pubSignals, 1632)))
            
            checkField(calldataload(add(_pubSignals, 1664)))
            
            checkField(calldataload(add(_pubSignals, 1696)))
            
            checkField(calldataload(add(_pubSignals, 1728)))
            
            checkField(calldataload(add(_pubSignals, 1760)))
            
            checkField(calldataload(add(_pubSignals, 1792)))
            
            checkField(calldataload(add(_pubSignals, 1824)))
            
            checkField(calldataload(add(_pubSignals, 1856)))
            
            checkField(calldataload(add(_pubSignals, 1888)))
            
            checkField(calldataload(add(_pubSignals, 1920)))
            
            checkField(calldataload(add(_pubSignals, 1952)))
            
            checkField(calldataload(add(_pubSignals, 1984)))
            
            checkField(calldataload(add(_pubSignals, 2016)))
            
            checkField(calldataload(add(_pubSignals, 2048)))
            
            checkField(calldataload(add(_pubSignals, 2080)))
            
            checkField(calldataload(add(_pubSignals, 2112)))
            
            checkField(calldataload(add(_pubSignals, 2144)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
