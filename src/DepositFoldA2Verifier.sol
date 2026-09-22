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

contract DepositFoldA2Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 20901312453931986983070371975751072566398443441960436590636518297529489694139;
    uint256 constant alphay  = 21697131480926335546036040991413011755153140230317664815989518148158724370212;
    uint256 constant betax1  = 4869487231785253308212429570614741443191402866210819877611793555879218415484;
    uint256 constant betax2  = 8494223465375928023871052138064538434365863294294498107508764578998413175191;
    uint256 constant betay1  = 13692213518456195651330939167802977105042737180923530430942149449680303886878;
    uint256 constant betay2  = 6329045905833941605730059747255744716806210507261219104759569890353192015645;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 9601970415856449080695136152815411116441611740360662129152490714795960788564;
    uint256 constant deltax2 = 18781083630875745457869536816316166342493056480347358237396896541373072651528;
    uint256 constant deltay1 = 1957895447810248267890542252186364986457658524228024378310979321757722848597;
    uint256 constant deltay2 = 13746637833693377802137456391836548438845662892740913372150996290576537426201;

    
    uint256 constant IC0x = 15224842209383197722275529189494148730162120941177374634270572744710281104486;
    uint256 constant IC0y = 6204375745271743700272264859858509749686791730874560605781254128610090979496;
    
    uint256 constant IC1x = 2176268772310823000873211748431617542531346051145960868378684613884642178734;
    uint256 constant IC1y = 12306819556325553105506872942459566336980167270029159648155244688420403745172;
    
    uint256 constant IC2x = 17571325694036079368006784464883435238174613415274020058580020547229627312757;
    uint256 constant IC2y = 17902106874058762137652001641036720992409733705764424986672966482323065432559;
    
    uint256 constant IC3x = 14870941876560244194585756388864831901077308732982577719916693360422734522777;
    uint256 constant IC3y = 3891604467024916516318516137721475341955743186614903569975099083214539660385;
    
    uint256 constant IC4x = 16150374780457257161193601605435258824433088632979011153644288199097948402563;
    uint256 constant IC4y = 3104155422652812296269884559100169112119975083279066560405959470938078761623;
    
    uint256 constant IC5x = 15687535852408633796697194715643109978311131096470833776642576664402126752364;
    uint256 constant IC5y = 6158787213320783597491514632992259048777214833595035787855663324895445904042;
    
    uint256 constant IC6x = 13821079944040527432088991418700146139761123228595851352716835897410451460074;
    uint256 constant IC6y = 2349098208404431168641501575434155125425939992039997838982716251303042420314;
    
    uint256 constant IC7x = 5571565412429867750705814890677532898764961459182941211309436901526143510744;
    uint256 constant IC7y = 20236613937196162319734651482739297758208648259390217100650071209717223247563;
    
    uint256 constant IC8x = 5431095801163335890230824752958906923359369113578125828725253084883333124054;
    uint256 constant IC8y = 19816982056317419171237328203406791368816222391033986644485350942412185391908;
    
    uint256 constant IC9x = 10291909412228099940245785718422393005619813602572041228077356728724181475433;
    uint256 constant IC9y = 5049403156505585130064535633310503795837815450835621838374575231375224626367;
    
    uint256 constant IC10x = 20573038153303472045101480524201126253473308034337081848537552722452971471181;
    uint256 constant IC10y = 11274236047951983647804350158477513827400093577121987857739739593854641095602;
    
    uint256 constant IC11x = 17849400838719094562344251954598146815873182253536721255754909329934276151616;
    uint256 constant IC11y = 7772978945090618868934795240045643538676690948149933859640803434968233933296;
    
    uint256 constant IC12x = 12064823992509453804489733293265977796306023451047994468480391520051419216890;
    uint256 constant IC12y = 16794288695192330099971700685457495946005956969019375392391233320420363680897;
    
    uint256 constant IC13x = 2628382230525393615075540304475961433827556246513750614456465435104451715122;
    uint256 constant IC13y = 20013103704494937916106336573906306946644922253241300071366557392166028074043;
    
    uint256 constant IC14x = 6068094507102469198630446589814956489445180274169535440057201323562995007868;
    uint256 constant IC14y = 20630234188804286811018383219411188034020882703941170331674354450637868960686;
    
    uint256 constant IC15x = 2044448150327584480493384662681574494104040843888548280751336220066085907511;
    uint256 constant IC15y = 2804735277202265718655920742779886294341488748411801727842698015634360026562;
    
    uint256 constant IC16x = 6230089547066170906604554405874676718497214949811935646140446922296212029915;
    uint256 constant IC16y = 10287413794796080974119973658747326067983949338392920683573697916397918247027;
    
    uint256 constant IC17x = 21692411715760704689113155726379694922349197482075763552242210872798115875187;
    uint256 constant IC17y = 5291833531553099421082993266716430381117935443586822578533168842821761522259;
    
    uint256 constant IC18x = 1597947154897004845201437005710543685368826145009553115378444978200016238373;
    uint256 constant IC18y = 19105966872444615118392393921420478555117846771146772016163740848619143883171;
    
    uint256 constant IC19x = 6936102873974500012038614866448923998972934808376437312152127587164181766526;
    uint256 constant IC19y = 18061805645786354089327237780664150312439996327755065898940101572108246589169;
    
    uint256 constant IC20x = 6020602605818519149034685914986201849291256028649178384442716674030048674423;
    uint256 constant IC20y = 16754600312688819842231596783022385117775417780420372670693430112974807389474;
    
    uint256 constant IC21x = 6619214645328277467231487707738256035685498794284417232756281241458260024542;
    uint256 constant IC21y = 7394011357802163342064379004097916963053223367447423621333432565116072870249;
    
    uint256 constant IC22x = 18090185226694368698916326194291603857649923254976428161869054155404654977154;
    uint256 constant IC22y = 13285394720637707704436562434305912574470851609523910767313866321529493282688;
    
    uint256 constant IC23x = 12243666700048680144732960406822268792368253136939157218264474039747120691110;
    uint256 constant IC23y = 3364311568298853507851836721699219648573603615957265976237987838600489407729;
    
    uint256 constant IC24x = 6668127623668986618550433461108487171984518130019411531450949742000786151904;
    uint256 constant IC24y = 3207842952659739518127031908712151620724769638843200984027911020257172094945;
    
    uint256 constant IC25x = 1002709097518947731810903794932680560920614726374834081596825130886890204146;
    uint256 constant IC25y = 8235909910570459797853795455393344787250290080821647407737129964005570057650;
    
    uint256 constant IC26x = 8306930666213838881360966965168398602341948410617336045865101785338929967518;
    uint256 constant IC26y = 7214456845273421778796541697300978215497092128711528143021131483173614427343;
    
    uint256 constant IC27x = 3810955203624017297929587778745664203532099843788471462000497152808603257190;
    uint256 constant IC27y = 765542504822086931692617763863174814513612931046709726176447840949578478156;
    
    uint256 constant IC28x = 814778839285988015986742120291173315673706650221161826371007306913819694759;
    uint256 constant IC28y = 6954539277897101501679985020497213198170746570914970972396726797183601569746;
    
    uint256 constant IC29x = 5365793690716858145845859073686348480970941911393859662248963849410972418861;
    uint256 constant IC29y = 15522045980860494054181122151909431501553878290553928695245385794072208967737;
    
    uint256 constant IC30x = 4008788341337764227921633998038963397961955862890162086722373309848602668860;
    uint256 constant IC30y = 15265826929070639438386250347458211957380943913514773212088649476480398374750;
    
    uint256 constant IC31x = 9059105922127604239419540026096130704289346765983572815427074791118286376253;
    uint256 constant IC31y = 13670285472996225589433130473065757751205850361197201114745410733645816915766;
    
    uint256 constant IC32x = 9671042729311146240112255000168862658746235377484732238159653640711093347428;
    uint256 constant IC32y = 14608991801275032328085651860882038137333680540318447107314304051913449919837;
    
    uint256 constant IC33x = 1329311219284461492085436402133088611979504589105741624852649327471973085176;
    uint256 constant IC33y = 1882839155633703125010941209939621169022062930374051213589790497186049223977;
    
    uint256 constant IC34x = 16429658753606727956337942637097290786167659221477406761457813015156138440926;
    uint256 constant IC34y = 4115575337796774843695291771443420453788576853481328509562352633500671348913;
    
    uint256 constant IC35x = 6725102382134476145285092457869963219821803608098254401476763670747914350917;
    uint256 constant IC35y = 3292128763060608359555403227249055916194563960509829094409025013770580746076;
    
    uint256 constant IC36x = 4437111151946114533565235631903423588867791665357489865909591723226596770528;
    uint256 constant IC36y = 10239101949852616258618611236721969964338329537353429136924488823819448828536;
    
    uint256 constant IC37x = 16584933604687090867450886169848392841255372422627190931686445125898189199908;
    uint256 constant IC37y = 14062273303865118677544270802263388910537515877765018747125027000517852655268;
    
    uint256 constant IC38x = 4222169087826649331262666183008491704909008543205391864057010801213399424354;
    uint256 constant IC38y = 9124157720749254874493811250482834937565788894106554195433331285233170259873;
    
    uint256 constant IC39x = 8990217391581450174235066820501697747776487162503135646636872336499696109219;
    uint256 constant IC39y = 5479566248723540670113522962785706689013564217410112537244029224043871263057;
    
    uint256 constant IC40x = 17442936963860284516912919618281438126543874340056986220186228558537790532420;
    uint256 constant IC40y = 20551065323093824203763368581557066069823201492691911790102432804299546766635;
    
    uint256 constant IC41x = 15248322396820344903734055000838740445179575368414584010362484338203636881161;
    uint256 constant IC41y = 4968332264936856018759349837052878179305705788618247794897687832935908311498;
    
    uint256 constant IC42x = 15736156232594700610069180257024329904444442725381213337389434663746733459355;
    uint256 constant IC42y = 314748531304324365254755703335482956329021698302968582010952423502055890473;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[42] calldata _pubSignals) public returns (bool) {
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
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
